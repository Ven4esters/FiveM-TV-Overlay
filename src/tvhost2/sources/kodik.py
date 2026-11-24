"""Kodik — плеер, на котором держится почти весь рунет-кино.

Схема, вскрытая замерами 23.11.2026 (ERR-071, ERR-079):

1. Страница `kodikplayer.com/<type>/<id>/<hash>/720p` содержит подписанные
   токены: `domain`, `d_sign`, `pd`, `pd_sign`, `ref`, `ref_sign`.
   Открытая напрямую — подменяется на «страницы не существует»
   (проверка `isIframe()`).

2. Внутри iframe плеер шлёт POST на `kodikplayer.com/ftor` с этими
   подписями + `type`, `hash`, `id`. Ответ — JSON:

       {"links": {"360": [{"src": "<шифр>"}],
                  "480": [{"src": "<шифр>"}],
                  "720": [{"src": "<шифр>"}]}}

3. Каждая ссылка зашифрована ДВАЖДЫ:
   - сначала ROT18 по латинице (сдвиг букв на 18);
   - затем base64.
   Проверено: расшифрованная строка — `https://p12.solodcdn.com/s/m/...`.

4. Ссылка ведёт на обычный HLS: `.../720.mp4:hls:manifest.m3u8`,
   внутри — TS-сегменты по 6 секунд. Играется нашим мостом как есть.

Почему не через браузер: /ftor отвечает по подписям из HTML, браузер
нужен только чтобы этих подписей не требовалось... но на деле подписи
уже в HTML. Поэтому разбираем HTTP-запросами: **0.5 секунды вместо 20**
и ни одного лишнего процесса Chromium (важно при 8 ГБ ОЗУ).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from ..core.errors import SourceFailedError
from ..core.models import Quality, SourceState, Stream, StreamKind, sort_qualities

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

FTOR_URL = "https://kodikplayer.com/ftor"
"""Эндпоинт со ссылками. Метод — POST, тело — application/x-www-form-urlencoded."""

HOSTS = ("kodikplayer.com", "kodik.info", "kodik.biz", "kodik.cc", "kodik.bz")
"""Зеркала Kodik: один и тот же плеер на разных доменах."""

DEFAULT_SITE = "animego.me"
"""Сайт-источник, от имени которого просим поток. Kodik требует его в подписи."""


# ----------------------------------------------------------------------
# расшифровка ссылок
# ----------------------------------------------------------------------


_A, _Z, _a, _z = ord("A"), ord("Z"), ord("a"), ord("z")
"""Границы латинских букв для ROT18 (цифры и знаки не трогаем)."""


def rot18(text: str) -> str:
    """Сдвиг латинских букв на 18 — часть шифра Kodik.

    Регистр сохраняется, цифры и знаки не трогаются. Декодирование и
    кодирование — одна и та же операция (18*2 = 36 = 26 + 10, но для
    букв это просто симметричный сдвиг).
    """
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if _A <= code <= _Z:
            out.append(chr((code - _A + 18) % 26 + _A))
        elif _a <= code <= _z:
            out.append(chr((code - _a + 18) % 26 + _a))
        else:
            out.append(ch)
    return "".join(out)


def decrypt_link(encoded: str) -> str:
    """Расшифровать `src` из ответа /ftor в обычный URL.

    Порядок обратный шифрованию: сначала ROT18, потом base64.
    """
    if encoded.startswith("http"):
        return encoded  # уже открытая ссылка (бывает у старых записей)
    unrotated = rot18(encoded)
    padded = unrotated + "=" * (-len(unrotated) % 4)
    try:
        raw = base64.b64decode(padded)
    except Exception as exc:
        raise SourceFailedError(
            "Kodik",
            "не удалось расшифровать ссылку на поток",
            fix="Пришлите ссылку разработчику — Kodik сменил шифр",
            state=SourceState.PARTIAL,
        ) from exc
    return raw.decode("latin-1")


# ----------------------------------------------------------------------
# разбор ссылки на плеер
# ----------------------------------------------------------------------

_PLAYER_RE = re.compile(
    r"kodik[a-z0-9\-]*\.(?:com|info|biz|cc|bz)/(?:(?:seria|video|film)/)?"
    r"(?P<type>[a-z]+)/(?P<id>\d+)/(?P<hash>[0-9a-f]{32})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class KodikRef:
    """Что вытащили из ссылки на плеер.

    `episode` — какая серия нужна, НОМЕР (1, 2, 3…), а не id.
    `0` — «как решит плеер»: для фильмов и для ссылок, где серии нет.

    Почему номер, а не id: у Kodik id серии берётся только со страницы
    плеера (`serial-series-box`), а в адресе лежит id СЕЗОНА. Хранить в
    ссылке номер удобнее: `?episode=3` читается человеком и переживает
    смену id на стороне Kodik.
    """

    kind: str  # "seria" | "video"
    video_id: str
    hash: str
    quality: str  # "720p" — заявленное качество; на поток не влияет
    host: str  # домен зеркала
    episode: int = 0


_EPISODE_RE = re.compile(r"[?&#]episode=(\d{1,4})", re.I)
"""Номер серии в адресе. Наш собственный параметр: Kodik его игнорирует,
мы читаем сами — в адресе плеера серии нет вообще."""


def parse_player_url(url: str) -> KodikRef | None:
    """Разобрать ссылку вида `/seria/1420137/<hash>/720p?episode=3`.

    Без `episode` сериал откроется на той серии, которую выберет сам Kodik
    (по своей подписи `vInfo`), и это НЕ обязательно первая — проверено
    живьём 25.11.2026.
    """
    match = _PLAYER_RE.search(url or "")
    if not match:
        return None
    host_match = re.search(r"(kodik[a-z0-9\-]*\.(?:com|info|biz|cc|bz))", url, re.I)
    quality_match = re.search(r"/(\d{3,4})p(?:/|$)", url or "")
    episode_match = _EPISODE_RE.search(url or "")
    return KodikRef(
        kind=match.group("type").lower(),
        video_id=match.group("id"),
        hash=match.group("hash"),
        quality=f"{quality_match.group(1)}p" if quality_match else "720p",
        host=host_match.group(1).lower() if host_match else "kodikplayer.com",
        episode=int(episode_match.group(1)) if episode_match else 0,
    )


# ----------------------------------------------------------------------
# получение ссылок
# ----------------------------------------------------------------------


def _http(url: str, data: dict[str, str] | None = None, referer: str = "") -> str:
    """Запрос к Kodik с правильными заголовками."""
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Origin": "https://kodikplayer.com",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer or f"https://{DEFAULT_SITE}/",
    }
    body = None
    if data is not None:
        body = urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", "replace")


_SIG_RE = {
    "domain": r'var domain = "([^"]*)"',
    "d_sign": r'var d_sign = "([^"]*)"',
    "pd": r'var pd = "([^"]*)"',
    "pd_sign": r'var pd_sign = "([^"]*)"',
    "ref": r'var ref = "([^"]*)"',
    "ref_sign": r'var ref_sign = "([^"]*)"',
}


def page_signatures(html: str) -> dict[str, str]:
    """Вытащить подписанные токены из HTML плеера."""
    found: dict[str, str] = {}
    for name, pattern in _SIG_RE.items():
        match = re.search(pattern, html)
        if match:
            found[name] = match.group(1)
    return found


_VINFO_RE = {
    "type": r"vInfo\.type\s*=\s*'([^']+)'",
    "hash": r"vInfo\.hash\s*=\s*'([^']+)'",
    "id": r"vInfo\.id\s*=\s*'(\d+)'",
}
"""Настоящие параметры серии со страницы плеера.

Зачем: у СЕРИАЛОВ kodikplayer.com подменяет хэш на свой серверный и кладёт
истинные значения в `vInfo`. Хэш из адреса (`/serial/37803/<hash>/720p`)
после подмены не принимается — /ftor отвечает 404. Проверено на
ani-media.online: адрес давал 404, а `vInfo` те же три значения — работали.

У фильмов подмены нет, и там `vInfo` совпадает с адресом, поэтому брать
его безопасно в любом случае.
"""


def page_vinfo(html: str) -> dict[str, str]:
    """Настоящие type/hash/id из HTML плеера. Пусто — значит не сериал."""
    found: dict[str, str] = {}
    for name, pattern in _VINFO_RE.items():
        match = re.search(pattern, html)
        if match:
            found[name] = match.group(1)
    return found


def fetch_links(ref: KodikRef, *, use_vinfo: bool = True) -> dict[str, list[dict[str, Any]]]:
    """Запросить /ftor и получить ссылки на все качества.

    Возвращает `{"720": [{"src": "<url>"}], "480": [...]}` — уже расшифрованные.

    `use_vinfo=False` — НЕ подменять переданные id/hash значениями `vInfo`.
    Нужно для серий: `vInfo` указывает на то, что плеер выбрал сам (у
    проверенного сериала — третью озвучку и не первую серию), и подмена
    молча отменяла нашу. Для фильмов и для ссылок «на сезон» подмена
    остаётся: там она как раз и спасает от 404.
    """
    player_url = f"https://{ref.host}/{ref.kind}/{ref.video_id}/{ref.hash}/720p"
    html = _http(player_url)
    sig = page_signatures(html)
    if not sig.get("d_sign") or not sig.get("ref_sign"):
        raise SourceFailedError(
            "Kodik",
            "страница плеера не отдала подписи доступа",
            fix=(
                "Ссылка могла устареть. Откройте её заново на сайте кинотеатра и скопируйте свежую"
            ),
            state=SourceState.PARTIAL,
        )

    payload = {
        "d": sig.get("domain", DEFAULT_SITE),
        "d_sign": sig.get("d_sign", ""),
        "pd": sig.get("pd", ref.host),
        "pd_sign": sig.get("pd_sign", ""),
        "ref": sig.get("ref", f"https://{DEFAULT_SITE}/"),
        "ref_sign": sig.get("ref_sign", ""),
        "bad_user": "false",
        "cdn_is_working": "false",
        "type": ref.kind,
        "hash": ref.hash,
        "id": ref.video_id,
        "info": "{}",
    }
    # Сериалы: истинные type/hash/id лежат в vInfo, а не в адресе.
    # Но только когда мы сами их не выбрали (см. `use_vinfo`).
    real = page_vinfo(html) if use_vinfo else {}
    if real.get("hash") and real.get("id") and real.get("type"):
        payload["type"] = real["type"]
        payload["hash"] = real["hash"]
        payload["id"] = real["id"]
    raw = _http(FTOR_URL, data=payload, referer=player_url)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceFailedError(
            "Kodik",
            "сервер вернул не JSON вместо ссылок",
            fix="Пришлите ссылку разработчику — Kodik сменил формат ответа",
            state=SourceState.PARTIAL,
        ) from exc

    links = data.get("links") or {}
    if not links:
        detail = data.get("detail") or data.get("error") or ""
        raise SourceFailedError(
            "Kodik",
            f"видео не отдано: {str(detail)[:120] or 'пустой список ссылок'}",
            fix="Проверьте ссылку в браузере — возможно, видео снято с показа",
            state=SourceState.PARTIAL,
        )

    decoded: dict[str, list[dict[str, Any]]] = {}
    for height, items in links.items():
        out: list[dict[str, Any]] = []
        for item in items:
            src = str(item.get("src") or "")
            if not src:
                continue
            try:
                out.append({**item, "src": decrypt_link(src)})
            except Exception:
                log.warning("Kodik: ссылка качества %s не расшифровалась", height)
        if out:
            decoded[str(height)] = out
    return decoded


def fetch_audio_tracks(ref: KodikRef, translations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Получить ссылки для каждой озвучки.

    У Kodik каждая озвучка — отдельный плеер со своим `hash`, поэтому
    запрашиваем /ftor для каждой. Ошибка одной не должна ломать остальные.
    """
    tracks: list[dict[str, Any]] = []
    for tr in translations:
        tr_hash = str(tr.get("hash") or "")
        if not tr_hash:
            continue
        tr_ref = KodikRef(
            kind=str(tr.get("type") or ref.kind),
            video_id=str(tr.get("id") or ref.video_id),
            hash=tr_hash,
            quality=ref.quality,
            host=ref.host,
        )
        try:
            links = fetch_links(tr_ref)
        except Exception as exc:
            log.info("Kodik: озвучка %s недоступна (%s)", tr.get("title"), str(exc)[:60])
            continue
        if links:
            tracks.append({"title": tr.get("title") or "Озвучка", "links": links})
    return tracks


def ensure_episode_exists(
    source_name: str,
    episodes: list[dict[str, Any]],
    translations: list[dict[str, Any]],
    translation_index: int,
    episode: int | None,
) -> None:
    """Есть ли у ВЫБРАННОЙ озвучки такая серия. Нет — отказ, а не подмена.

    Считаем по своей озвучке, а не по общему списку: у одной 8 серий, у
    другой — 4 (проверено живьём 25.11.2026: Amazing Dubbing и Solomon по 8,
    Домик Сумасшедших Дабберов — 4). Иначе человек попросит 5-ю серию и
    получит пустоту или чужую — ровно то, на что жаловались.
    """
    if not episode or not episodes:
        return
    _number, track = pick_translation(translations, translation_index)
    count = str((track or {}).get("episode_count") or "")
    limit = int(count) if count.isdigit() else 0
    has_number = str(episode) in {str(e.get("episode")) for e in episodes}
    if has_number and not (limit and episode > limit):
        return
    raise SourceFailedError(
        source_name,
        f"у выбранной озвучки нет {episode}-й серии",
        fix=f"В этой озвучке серий: {limit}" if limit else f"Доступны серии: {len(episodes)}",
        state=SourceState.PARTIAL,
    )


def pick_translation(
    translations: list[dict[str, Any]], index: int | None
) -> tuple[int, dict[str, Any] | None]:
    """Какая озвучка выбрана: (номер, запись). Пустая — плеер без озвучек.

    Правило одно на весь файл, чтобы поток и проверка «есть ли такая серия»
    не разошлись: просили номер — берём его (если в списке есть), не просили
    или ошиблись — ту, что Kodik пометил `selected`, иначе первую.
    """
    if not translations:
        return 0, None
    if index is not None and 0 <= index < len(translations):
        return index, translations[index]
    for number, track in enumerate(translations):
        if track.get("selected"):
            return number, track
    return 0, translations[0]


def parse_translations(html: str) -> list[dict[str, Any]]:
    """Достать список озвучек из HTML плеера.

    Реальная разметка Kodik (проверено 23.11.2026) — `<option>` внутри
    `.movie-translations-box`:

        <option value="729" data-id="729" data-media-id="1420137"
                data-media-hash="746cbbd9..." data-media-type="seria"
                data-title="СВ-Дубль" selected>СВ-Дубль</option>

    Каждая озвучка — отдельный поток со своим hash, поэтому переключение
    озвучки требует повторного запроса /ftor с этим hash.
    """
    found: list[dict[str, Any]] = []
    option_re = re.compile(
        r"<option\b(?P<attrs>[^>]*)>(?P<text>[^<]*)</option>", re.IGNORECASE | re.DOTALL
    )
    attr_re = re.compile(r'(data-[a-z\-]+|value)\s*=\s*"([^"]*)"', re.IGNORECASE)
    for match in option_re.finditer(html):
        attrs = {k.lower(): v for k, v in attr_re.findall(match.group("attrs"))}
        media_hash = attrs.get("data-media-hash", "")
        if not media_hash:
            continue  # это не озвучка (или старая разметка)
        title = (attrs.get("data-title") or match.group("text") or "").strip()
        found.append(
            {
                "id": attrs.get("data-id", attrs.get("value", "")),
                "title": title,
                "hash": media_hash,
                "type": attrs.get("data-media-type", "seria"),
                "media_id": attrs.get("data-media-id", ""),
                "translation_type": attrs.get("data-translation-type", ""),
                # Сколько серий в этой озвучке. У сайта бывает по-разному:
                # у одной озвучки 8 серий, у другой — 4. Нужно, чтобы не
                # открыть «5 серию» там, где их всего четыре.
                "episode_count": attrs.get("data-episode-count", ""),
                "selected": "selected" in match.group("attrs").lower(),
            }
        )
    return found


# ----------------------------------------------------------------------
# серии и сезоны
# ----------------------------------------------------------------------

_SERIAL_BLOCK_RE = re.compile(r'<div class="serial-panel.*?</div>\s*</div>', re.S)
"""Блок с селектами сезона, серии и озвучки — единственное место, где они есть."""

_BOX_RE = {
    "seasons": re.compile(r'<div class="serial-seasons-box".*?</select>', re.S),
    "episodes": re.compile(r'<div class="serial-series-box".*?</select>', re.S),
    "translations": re.compile(r'<div class="serial-translations-box".*?</select>', re.S),
}
"""Три выпадающих списка плеера. Ищем по классу обёртки, а не по порядку:
порядок в разных версиях плеера разный."""

_OPTION_RE = re.compile(r"<option\b(?P<attrs>[^>]*)>(?P<text>[^<]*)</option>", re.S | re.I)
_ATTR_RE = re.compile(r'([a-zA-Z\-]+)\s*=\s*"([^"]*)"')


def _options(html: str, box: str) -> list[dict[str, str]]:
    """Все `<option>` из указанного списка плеера, с атрибутами."""
    found = _BOX_RE[box].search(html or "")
    if not found:
        return []
    items: list[dict[str, str]] = []
    for match in _OPTION_RE.finditer(found.group(0)):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(match.group("attrs"))}
        attrs["text"] = (match.group("text") or "").strip()
        items.append(attrs)
    return items


def pick_episode(episodes: list[dict[str, Any]], ref: KodikRef) -> tuple[str, str]:
    """id и hash нужной серии. Пустая пара — оставить как в адресе.

    В адресе `id` и `hash` принадлежат СЕЗОНУ, а не серии, и Kodik сам
    выбирает, что включить — по своей подписи `vInfo`. Проверено живьём
    25.11.2026 на `/serial/35906/10ea0c…/720p`: `vInfo.id` = 872800, а это
    ВОСЬМАЯ серия из восьми. Отсюда жалоба «воспроизводится не первая».

    Если же ссылка уже ведёт на серию (её id есть в списке — так бывает
    у адресов, скопированных из самого плеера), ничего не подменяем.
    """
    if not episodes:
        return "", ""
    if any(str(e.get("id")) == ref.video_id for e in episodes):
        return "", ""
    wanted = str(ref.episode or 1)
    picked = next(
        (e for e in episodes if str(e.get("episode")) == wanted),
        episodes[0],
    )
    # Номер, которого в списке нет, — не повод открыть другую серию молча:
    # человек увидит не то, что просил. Отдаём первую только когда номера
    # не было вовсе.
    if ref.episode and str(picked.get("episode")) != wanted:
        picked = {"id": "", "hash": ""}
    return str(picked.get("id") or ""), str(picked.get("hash") or "")


def parse_episodes(html: str) -> list[dict[str, Any]]:
    """Серии сериала: `[{season, episode, title, id, hash}, …]`.

    Зачем это вообще нужно — выяснено живьём 25.11.2026 на ссылке
    `/serial/35906/10ea0c…/720p` с dorama-land.top. Там `id` и `hash` в
    АДРЕСЕ принадлежат не серии, а сезону:

        адрес:     /serial/35906/10ea0c4687d90c530d45d68ac3b64b05/720p
        сезон:     value=1  data-serial-id=35906  data-serial-hash=10ea0c…
        1 серия:   value=1  data-id=855794  data-hash=68cfae61…

    То есть запрос по адресу отдаёт «сериал целиком», и Kodik сам решает,
    что включить — а решает он по своей подписи `vInfo` (872800/711772…),
    и это вообще третья озвучка. Отсюда и «играет не та серия», и «перевод
    слетает на другой». Поэтому серию надо брать из `serial-series-box`
    явно, а не полагаться на адрес.

    Сезон у Kodik в URL не участвует: серии всех сезонов лежат в одном
    списке, а номер сезона — только подпись. Поэтому `season` берём из
    выбранного сезона, чтобы показать человеку «1 сезон · 3 серия».
    """
    markup = html or ""
    season_text = "1"
    seasons = _options(markup, "seasons")
    for item in seasons:
        if "selected" in item:
            season_text = item.get("value", "1") or "1"
            break
    else:
        if seasons:
            season_text = seasons[0].get("value", "1") or "1"

    episodes: list[dict[str, Any]] = []
    for item in _options(markup, "episodes"):
        number = item.get("value", "")
        ident = item.get("data-id", "")
        media_hash = item.get("data-hash", "")
        if not ident or not media_hash:
            continue
        episodes.append(
            {
                "season": season_text,
                "episode": number,
                "title": item.get("data-title") or item.get("text") or f"{number} серия",
                "id": ident,
                "hash": media_hash,
            }
        )
    return episodes


def translation_episodes(html: str) -> list[dict[str, Any]]:
    """Озвучки с их `id`/`hash` — для переключения перевода у сериала."""
    tracks: list[dict[str, Any]] = []
    for item in _options(html or "", "translations"):
        media_hash = item.get("data-media-hash", "")
        if not media_hash:
            continue
        tracks.append(
            {
                "id": item.get("data-id", item.get("value", "")),
                "title": item.get("data-title") or item.get("text") or "",
                "hash": media_hash,
                "type": item.get("data-media-type", "serial"),
                "media_id": item.get("data-media-id", ""),
                "translation_type": item.get("data-translation-type", ""),
                "episode_count": item.get("data-episode-count", ""),
                "selected": "selected" in item,
            }
        )
    return tracks


def all_urls(url: str, html: str) -> list[str]:
    """Адреса всех серий, какие плеер показал в списке.

    Нужны приложению, чтобы предложить «следующая серия», а не заставлять
    человека искать ссылку заново.
    """
    ref = parse_player_url(url)
    if ref is None:
        return []
    return [
        f"https://{ref.host}/serial/{ep['id']}/{ep['hash']}/{ref.quality}"
        for ep in parse_episodes(html)
    ]


def url_with_episode(url: str, number: int) -> str:
    """Та же ссылка, но с номером серии в параметре `?episode=N`.

    Зачем именно в адресе: сессия хранит ссылку, и по ней же история
    просмотров узнаёт запись. Положив номер в адрес, мы получаем, что
    «3 серия» — это отдельная запись, и вернуться к ней можно одним
    нажатием. Kodik параметр игнорирует, читаем его только мы.
    """
    parts = urllib.parse.urlsplit(url or "")
    if not parts.netloc:
        return url
    params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    params = [(k, v) for k, v in params if k.lower() != "episode"]
    if number > 0:
        params.append(("episode", str(number)))
    query = urllib.parse.urlencode(params)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


# ----------------------------------------------------------------------
# источник для реестра
# ----------------------------------------------------------------------


class KodikSource:
    """Источник Kodik: ссылка на плеер → HLS-поток со всеми качествами."""

    name = "Kodik"
    state = SourceState.OK
    note = "кинопоиск рунета, все озвучки"
    kind = StreamKind.HLS

    def matches(self, url: str) -> bool:
        return parse_player_url(url) is not None

    def url_with_episode(self, url: str, number: int) -> str:
        """Как запомнить номер серии в ссылке. Нужно сессии и истории.

        Метод есть только у источников, которые умеют переключать серии:
        сессия спрашивает его через `getattr` и не знает имён сайтов.
        """
        return url_with_episode(url, number)

    def resolve(self, url: str, translation_index: int = 0, episode: int | None = None) -> Stream:
        """Разобрать ссылку. translation_index — какая озвучка нужна.

        `episode` — номер серии с единицы. Если он передан, ссылка получает
        `?episode=N` (это наш параметр, Kodik его игнорирует): так номер не
        теряется, пока ссылка ходит по сессии и истории. Если не передан,
        берётся номер из самой ссылки, а без него — первая серия.


        У Kodik каждая озвучка — отдельный поток со своим hash, поэтому
        «переключить дорожку» нельзя: нужен повторный запрос /ftor с hash
        выбранной озвучки. Индекс, а не id: так вызывающему коду не надо
        знать про hash вообще.
        """
        if episode is not None:
            url = url_with_episode(url, episode)
        ref = parse_player_url(url)
        if ref is None:
            raise SourceFailedError(
                self.name,
                "не удалось разобрать ссылку на плеер",
                fix="Нужна ссылка вида kodikplayer.com/seria/<id>/<hash>/720p",
                state=SourceState.PARTIAL,
            )

        player_url = f"https://{ref.host}/{ref.kind}/{ref.video_id}/{ref.hash}/720p"
        html = _http(player_url)
        translations = parse_translations(html)

        episodes = parse_episodes(html)
        episode_id, episode_hash = pick_episode(episodes, ref)
        # ---- озвучка --------------------------------------------------
        # Каждая озвучка — отдельный поток со своим hash. Номер берём из
        # СВОЕГО списка (порядок тот же, что в `audio_tracks`), а не из
        # `vInfo`: `vInfo` указывает на ту озвучку, что Kodik выбрал сам,
        # и именно из-за неё «перевод слетал» после смены серии.
        chosen = ref
        if translations:
            _index, track = pick_translation(translations, translation_index)
            assert track is not None  # translations непуст → track есть
            chosen = KodikRef(
                kind=str(track.get("type") or ref.kind),
                video_id=str(track.get("media_id") or ref.video_id),
                hash=str(track.get("hash") or ref.hash),
                quality=ref.quality,
                host=ref.host,
                episode=ref.episode,
            )

        # Серия главнее сезона: у Kodik серия — самостоятельный поток
        # (`data-id`/`data-hash` из списка), и только она даёт нужную серию.
        if episode_id and episode_hash:
            payload_ref = KodikRef(
                kind="seria",
                video_id=episode_id,
                hash=episode_hash,
                quality=chosen.quality,
                host=chosen.host,
                episode=ref.episode,
            )
        else:
            payload_ref = chosen
        links = fetch_links(payload_ref, use_vinfo=not (episode_id and episode_hash))

        qualities: list[Quality] = []
        for height_text, items in links.items():
            try:
                height = int(height_text)
            except ValueError:
                continue
            url_stream = str(items[0].get("src") or "")
            if not url_stream:
                continue
            width = round(height * 16 / 9)
            # Битрейт Kodik не сообщает. Оценка по площади кадра — нижняя
            # граница для H.264, чтобы ABR не застрял на низком уровне.
            bitrate = max(1, round(width * height * 30 / 10_000))
            qualities.append(
                Quality(
                    height=height,
                    width=width,
                    bitrate_kbps=bitrate,
                    url=url_stream,
                )
            )

        ensure_episode_exists(self.name, episodes, translations, translation_index, ref.episode)
        if not qualities:
            raise SourceFailedError(
                self.name,
                "поток не отдался ни в одном качестве",
                fix="Проверьте ссылку в браузере: если там видео играет — пришлите её разработчику",
                state=SourceState.PARTIAL,
            )

        # Серии — для пульта: без списка человек не может переключить серию,
        # не возвращаясь на сайт. Номер текущей знает только ссылка.
        episode_list = tuple(
            {
                "season": e.get("season", ""),
                "episode": e.get("episode", ""),
                "title": e.get("title", ""),
            }
            for e in episodes
        )
        current_episode = ref.episode
        if not current_episode and episodes:
            # Ссылка без номера: Kodik сам выбрал серию по `vInfo`. Найти её
            # можно только по id из подписи — если он там есть.
            vinfo_id = str(page_vinfo(html).get("id") or "")
            match = next((e for e in episodes if str(e.get("id")) == vinfo_id), None)
            number = str(match.get("episode", "")) if match else ""
            current_episode = int(number) if number.isdigit() else 1

        title = _title_from_html(html) or "Kodik"
        if episodes and ref.episode:
            label = next(
                (e.get("title") for e in episodes if str(e.get("episode")) == str(ref.episode)),
                f"{ref.episode} серия",
            )
            title = f"{title} · {label}"
        return Stream(
            source=self.name,
            title=title,
            kind=StreamKind.HLS,
            qualities=sort_qualities(qualities),
            state=self.state,
            audio_tracks=tuple(translations),
            episodes=episode_list,
            episode=current_episode if episodes else 0,
            can_switch_episode=len(episode_list) > 1,
        )


def _title_from_html(html: str) -> str:
    """Название видео, если Kodik его сообщает (обычно нет — отдаёт сайт)."""
    match = re.search(r"movieTitle\s*[:=]\s*[\"']([^\"']+)[\"']", html)
    if match:
        return match.group(1).strip()
    match = re.search(r'"title"\s*:\s*"([^"]{3,80})"', html)
    return match.group(1).strip() if match else ""


def embed_for_site(ref: KodikRef, site: str = DEFAULT_SITE) -> str:
    """URL плеера с указанием сайта: нужен, чтобы подпись принималась."""
    params = urlencode({"d": site})
    return f"https://{ref.host}/{ref.kind}/{ref.video_id}/{ref.hash}/720p?{params}"


def first_url(links: dict[str, list[dict[str, Any]]]) -> str:
    """Ссылка на лучшее качество — для быстрой проверки живости."""
    for height in sorted(links, key=lambda h: int(h) if h.isdigit() else 0, reverse=True):
        items = links[height]
        if items and items[0].get("src"):
            return str(items[0]["src"])
    return ""


__all__ = [
    "HOSTS",
    "KodikRef",
    "KodikSource",
    "decrypt_link",
    "embed_for_site",
    "ensure_episode_exists",
    "fetch_audio_tracks",
    "fetch_links",
    "first_url",
    "page_signatures",
    "parse_episodes",
    "parse_player_url",
    "parse_translations",
    "pick_episode",
    "pick_translation",
    "rot18",
    "translation_episodes",
    "url_with_episode",
]
