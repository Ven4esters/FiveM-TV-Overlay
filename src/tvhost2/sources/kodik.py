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
    """Что вытащили из ссылки на плеер."""

    kind: str  # "seria" | "video"
    video_id: str
    hash: str
    quality: str  # "720p" — заявленное качество; на поток не влияет
    host: str  # домен зеркала


def parse_player_url(url: str) -> KodikRef | None:
    """Разобрать ссылку вида `/seria/1420137/<hash>/720p`."""
    match = _PLAYER_RE.search(url or "")
    if not match:
        return None
    host_match = re.search(r"(kodik[a-z0-9\-]*\.(?:com|info|biz|cc|bz))", url, re.I)
    quality_match = re.search(r"/(\d{3,4})p(?:/|$)", url or "")
    return KodikRef(
        kind=match.group("type").lower(),
        video_id=match.group("id"),
        hash=match.group("hash"),
        quality=f"{quality_match.group(1)}p" if quality_match else "720p",
        host=host_match.group(1).lower() if host_match else "kodikplayer.com",
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


def fetch_links(ref: KodikRef) -> dict[str, list[dict[str, Any]]]:
    """Запросить /ftor и получить ссылки на все качества.

    Возвращает `{"720": [{"src": "<url>"}], "480": [...]}` — уже расшифрованные.
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
    real = page_vinfo(html)
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
                "selected": "selected" in match.group("attrs").lower(),
            }
        )
    return found


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

    def resolve(self, url: str, translation_index: int = 0) -> Stream:
        """Разобрать ссылку. translation_index — какая озвучка нужна.

        У Kodik каждая озвучка — отдельный поток со своим hash, поэтому
        «переключить дорожку» нельзя: нужен повторный запрос /ftor с hash
        выбранной озвучки. Индекс, а не id: так вызывающему коду не надо
        знать про hash вообще.
        """
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

        # Выбранная озвучка: у неё свой hash, и /ftor надо спрашивать по нему.
        # Если индекс вне списка — берём ту, что плеер выбрал сам (selected),
        # иначе первую.
        chosen = ref
        if translations:
            index = translation_index if 0 <= translation_index < len(translations) else 0
            if not 0 <= translation_index < len(translations):
                index = next((i for i, t in enumerate(translations) if t.get("selected")), 0)
            track = translations[index]
            chosen = KodikRef(
                kind=str(track.get("type") or ref.kind),
                video_id=str(track.get("media_id") or ref.video_id),
                hash=str(track.get("hash") or ref.hash),
                quality=ref.quality,
                host=ref.host,
            )
        links = fetch_links(chosen)

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

        if not qualities:
            raise SourceFailedError(
                self.name,
                "поток не отдался ни в одном качестве",
                fix="Проверьте ссылку в браузере: если там видео играет — пришлите её разработчику",
                state=SourceState.PARTIAL,
            )

        title = _title_from_html(html) or "Kodik"
        return Stream(
            source=self.name,
            title=title,
            kind=StreamKind.HLS,
            qualities=sort_qualities(qualities),
            state=self.state,
            audio_tracks=tuple(translations),
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
    "fetch_audio_tracks",
    "fetch_links",
    "first_url",
    "page_signatures",
    "parse_player_url",
    "parse_translations",
    "rot18",
]
