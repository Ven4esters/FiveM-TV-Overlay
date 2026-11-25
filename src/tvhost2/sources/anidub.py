"""anidub.com — обёртка над балансером anivids (этап 10 плана).

Что важно понимать про этот сайт:

1. **anidub не отдаёт поток сам.** Он показывает серию через iframe плеера
   `player*.ladonyvesna2005.info` (раньше это был `cdnvideohub`, у него
   сменился и хост, и имя: из ТЗ он уже не совпадает с реальностью).
   Домен плеера цифровой (`player26`, `player31`, …) и, видимо, меняется
   вместе с балансировкой, поэтому разбираем ссылку из iframe, а не
   подставляем известный хост.

2. **У плеера две ступени.** `index.php?v=/s9/<hash>` — это HTML-страница с
   <video data-stream-src="vid.php?v=%2Fs9%2F<hash>">, а `vid.php` отдаёт
   master-плейлист с подписанными ссылками на anivids.link. Проверено:
   плейлист содержит `fhd.mp4` (1280x720) и `sd.mp4` (854x480), в каждом
   сотни `.ts`-сегментов, звук внутри — как у Rutube, склейка не нужна.

3. **Подпись живёт часами, но привязана к запросу.** Перезапрос `vid.php`
   отдаёт другой CDN (`cdn1…cdn5`), другой `vodN` и новый `md5`. Протухшая
   ссылка отвечает 403 «Angie». Значит: резолв делаем заново в момент
   открытия, а сегменты забирает наш мост — он ходит свежими запросами сам.

4. **ffmpeg требует User-Agent.** На запрос с `Lavf/…` CDN отвечает 403,
   с Firefox-UA — 200. Для браузера это неважно (мост подставляет свои
   заголовки), но при замере громкости ffmpeg идёт напрямую, поэтому
   заголовки кладём в `Stream.headers`.

Защиты вроде Anubis здесь нет: страница отдаётся обычным GET, браузерная
сессия не нужна. Это делает anidub самым дешёвым источником из второго
контура.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse

from ..core.errors import SourceFailedError
from ..core.models import Quality, SourceState, Stream, StreamKind, sort_qualities
from ..web.fetch import FetchError, fetch

log = logging.getLogger(__name__)

SITE = "anidub.com"

_PAGE_RE = re.compile(r"^https?://(?:www\.)?anidub\.com/", re.IGNORECASE)
"""Ссылка на страницу серии. Внутри бывают и /anime_ongoing/, и /12254-….html."""

_IFRAME_RE = re.compile(r'<iframe[^>]+src="(https?://[^"]+)"', re.IGNORECASE)
_SPAN_RE = re.compile(r'<span[^>]+data="(https?://[^"]+)"', re.IGNORECASE)
"""Плееры anidub ставит дважды: скрытым iframe и вкладкой со ссылкой в data.
Обе ведут на один и тот же адрес, но у разных серий заполнено то одно, то
другое — поэтому смотрим оба."""

_STREAM_SRC_RE = re.compile(r'data-stream-src="([^"]+)"', re.IGNORECASE)
_TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.IGNORECASE)
_STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF:([^\n]+)\n([^\n#]+)")
_RESOLUTION_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)")
_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)")

TITLE_SUFFIX = " смотреть онлайн"
"""Хвост в <title> у anidub: его не показываем в оверлее."""


def is_anidub(url: str) -> bool:
    """Ссылка ведёт на страницу anidub?"""
    return bool(_PAGE_RE.match(url or ""))


def player_url(html: str) -> str:
    """Адрес плеера со страницы серии.

    Порядок: сначала iframe (он же и играет), потом вкладка `data`.
    Относительные адреса не поддерживаем — anidub всегда даёт абсолютные,
    а гадать про базу значит ломаться молча, если это изменится.
    """
    for pattern in (_IFRAME_RE, _SPAN_RE):
        for match in pattern.finditer(html or ""):
            candidate = match.group(1).strip()
            if "player" in candidate.lower():
                return candidate.replace("&amp;", "&")
    return ""


def stream_path(html: str) -> str:
    """Внутренний путь потока из HTML плеера (`vid.php?v=…`).

    Именно относительный адрес: `data-stream-src="vid.php?v=%2Fs9%2F<hash>"`.
    Абсолютный получается склейкой с адресом плеера — так переживём смену
    цифрового домена `player26` → `player31`.
    """
    match = _STREAM_SRC_RE.search(html or "")
    return match.group(1).strip().replace("&amp;", "&") if match else ""


def playlist_episodes(html: str) -> list[dict[str, str]]:
    """Серии из плейлиста плеера (`ladonyvesna…`): подпись, ссылка, ключ.

    Почему отдельный разбор: у этого плеера нет ни JSON-API, ни списка в
    разметке страницы — серии лежат объектом внутри скрипта:

        {"locale":"ru","hasSeries":true,…,
         "episodes":[{"link":"/s2/88641d…","key":"0697…","label":"1 серия"}, …]}

    Проверено 25.11.2026 на «Покемон. Горизонты»: 157 серий, и раньше мы
    отдавали пульту НОЛЬ — жалоба «серии нельзя переключить» на anidub.com.
    Озвучек у этого плеера нет: одна дорожка на серию.
    """
    text = html or ""
    marker = '"episodes"'
    at = text.find(marker)
    if at < 0:
        return []
    start = text.rfind("{", 0, at)
    if start < 0:
        return []
    depth = 0
    end = -1
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    if end < 0:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    episodes = data.get("episodes")
    if not isinstance(episodes, list):
        return []
    result: list[dict[str, str]] = []
    for item in episodes:
        if not isinstance(item, dict):
            continue
        if not item.get("link"):
            continue
        result.append(
            {
                "label": str(item.get("label") or ""),
                "link": str(item.get("link") or ""),
                "key": str(item.get("key") or ""),
            }
        )
    return result


def episode_number(label: str) -> int:
    """Номер серии из подписи «7 серия» → 7. 0 — номер не разобрали."""
    match = re.search(r"\d+", label or "")
    return int(match.group()) if match else 0


def pick_episode_url(player: str, episodes: list[dict[str, str]], number: int) -> str:
    """Адрес плеера с нужной серией — `?episode=N`, как это делает сам сайт.

    Плеер принимает номер параметром и сам подставляет ссылку и ключ серии
    (проверено: `&episode=3` открывает третью). Поэтому не подменяем `v=`
    руками — иначе при смене вёрстки мы бы молча ломались.
    """
    if not episodes or number <= 0:
        return player
    parts = urllib.parse.urlsplit(player)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    query = [(k, v) for k, v in query if k.lower() != "episode"]
    query.append(("episode", str(number)))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


def parse_master(playlist: str) -> tuple[Quality, ...]:
    """Master-плейлист anivids → качества.

    Здесь есть и RESOLUTION, и BANDWIDTH, поэтому размеры и битрейт берём
    настоящие, без оценок «по площади кадра». Если BANDWIDTH не указан,
    считаем нижнюю границу для H.264, чтобы ABR не застрял (ERR-067).
    """
    items: list[Quality] = []
    for match in _STREAM_INF_RE.finditer(playlist or ""):
        attrs, link = match.group(1), match.group(2).strip()
        resolution = _RESOLUTION_RE.search(attrs)
        if not link.startswith("http") or not resolution:
            continue
        width, height = int(resolution.group(1)), int(resolution.group(2))
        bandwidth = _BANDWIDTH_RE.search(attrs)
        kbps = round(int(bandwidth.group(1)) / 1000) if bandwidth else 0
        if kbps <= 0:
            kbps = max(1, round(width * height * 30 / 10_000))
        items.append(Quality(height=height, width=width, bitrate_kbps=kbps, url=link))
    return sort_qualities(items)


def clean_title(html: str) -> str:
    """Название серии из <title> страницы, без хвоста «смотреть онлайн…»."""
    match = _TITLE_RE.search(html or "")
    if not match:
        return ""
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    cut = title.lower().find(TITLE_SUFFIX)
    if cut > 0:
        title = title[:cut].strip()
    return title


class AnidubSource:
    """anidub.com: страница серии → плеер → master anivids → HLS."""

    name = "anidub"
    state = SourceState.OK
    note = "аниме, до 720p, без браузера"
    kind = StreamKind.HLS

    def matches(self, url: str) -> bool:
        return is_anidub(url)

    def url_with_episode(self, url: str, number: int) -> str:
        """Запомнить номер серии в адресе — `?episode=N`, как у Kodik.

        Без этого смена озвучки (повторный разбор) начинала бы с первой
        серии: источник не знал, какая открыта. У этого сайта озвучек нет,
        но правило общее — сессия спрашивает метод, не зная имён сайтов.
        """
        parts = urllib.parse.urlsplit(url or "")
        if not parts.netloc:
            return url
        params = [
            (k, v)
            for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() != "episode"
        ]
        if number > 0:
            params.append(("episode", str(number)))
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(params), parts.fragment)
        )

    def resolve(
        self,
        url: str,
        translation_index: int = 0,  # noqa: ARG002 — озвучек тут нет, параметр общий
        episode: int | None = None,
    ) -> Stream:
        """Ссылка на страницу серии → поток.

        Ступени ровно как у сайта: страница → iframe плеера → HTML плеера
        (`data-stream-src`) → master-плейлист. Каждая ступень проверяется
        отдельно: если сломается одна, в журнале будет видно какая, а не
        «anidub не работает».
        """
        try:
            page = fetch(url).text()
        except FetchError as exc:
            raise SourceFailedError(
                self.name,
                f"страница не открылась: {exc.reason}",
                fix="Проверьте ссылку и интернет",
                state=SourceState.BROKEN,
            ) from exc

        embed = player_url(page)
        if not embed:
            raise SourceFailedError(
                self.name,
                "на странице нет плеера",
                fix="Нужна страница серии: anidub.com/<id>-<название>.html",
                state=SourceState.PARTIAL,
            )

        try:
            player = fetch(embed).text()
        except FetchError as exc:
            raise SourceFailedError(
                self.name,
                f"плеер не открылся: {exc.reason}",
                fix="Балансер anivids мог переехать — пришлите ссылку разработчику",
                state=SourceState.PARTIAL,
            ) from exc

        # Серии: список берём из плейлиста плеера, а нужную открываем
        # параметром `?episode=N` — ровно так делает сам сайт.
        episodes = playlist_episodes(player)
        current_episode = 0
        if episodes:
            wanted = int(episode or 0)
            if wanted <= 0:
                wanted = episode_number(episodes[0]["label"]) or 1
            numbers = [episode_number(item["label"]) for item in episodes]
            if wanted not in numbers:
                raise SourceFailedError(
                    self.name,
                    f"у этого тайтла нет {wanted}-й серии",
                    fix=f"Доступны серии {min(n for n in numbers if n)}–{max(numbers)}",
                    state=SourceState.PARTIAL,
                )
            current_episode = wanted
            player = pick_episode_url(embed, episodes, wanted)
            try:
                player = fetch(player).text()
            except FetchError as exc:
                raise SourceFailedError(
                    self.name,
                    f"плеер не открылся на {wanted}-й серии: {exc.reason}",
                    fix="Пришлите ссылку разработчику",
                    state=SourceState.PARTIAL,
                ) from exc

        path = stream_path(player)
        if not path:
            raise SourceFailedError(
                self.name,
                "плеер не отдал адрес потока",
                fix="Сменилась вёрстка плеера — пришлите ссылку разработчику",
                state=SourceState.BROKEN,
            )

        master_url = urllib.parse.urljoin(embed, path)
        try:
            master = fetch(master_url).text()
        except FetchError as exc:
            raise SourceFailedError(
                self.name,
                f"поток не отдался: {exc.reason}",
                fix="Файл мог быть удалён с CDN — проверьте в браузере",
                state=SourceState.PARTIAL,
            ) from exc

        qualities = parse_master(master)
        if not qualities:
            raise SourceFailedError(
                self.name,
                "в потоке нет ни одного качества",
                fix="Балансер отдал неожиданный ответ — пришлите ссылку разработчику",
                state=SourceState.BROKEN,
            )

        log.info("anidub: %s → %s качеств", url, len(qualities))
        return Stream(
            source=self.name,
            title=(clean_title(page) or "anidub")
            + (f" — серия {current_episode}" if current_episode else ""),
            kind=self.kind,
            qualities=qualities,
            # CDN anivids отвечает 403 на ffmpeg-отпечаток: замер громкости
            # идёт мимо моста, поэтому заголовки обязаны ехать вместе с потоком.
            # UA именно Firefox, а не Chrome — проверено перебором:
            # CDN anivids отвечает 403 на «AppleWebKit/… Chrome/131» и 200
            # на Firefox. Это тот же UA, что fetch() ставит по умолчанию
            # для незнакомых хостов, поэтому и замер громкости через ffmpeg
            # обязан слать ровно его, иначе получит 403.
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) "
                    "Gecko/20100101 Firefox/121.0"
                ),
                "Referer": "https://anidub.com/",
            },
            # Серии — по общему контракту `Stream.episodes`. Озвучек у этого
            # плеера нет: одна дорожка на серию, поэтому `audio_tracks` пуст.
            episodes=tuple(
                {
                    "season": "",
                    "episode": str(episode_number(item["label"])),
                    "title": item["label"],
                }
                for item in episodes
            ),
            episode=current_episode,
            can_switch_episode=len(episodes) > 1,
            state=self.state,
        )
