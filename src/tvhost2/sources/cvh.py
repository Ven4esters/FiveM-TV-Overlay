"""cdnvideohub (`cvh`) — общий плеер для РАЗНЫХ сайтов, а не для одного.

Зачем отдельный модуль: один и тот же плеер стоит на anidub.org и на
yummyanime. Различий между ними ровно два числа — `aggr` (у anidub `cvh`,
у yummy `mali`) и `pub` (19 и 745). Всё остальное совпадает до байта:
плейлист, ответ `/video`, поля `mpeg*Url`. Держать два разбора значит
чинить одно и то же дважды — ровно от этого и было требование заказчика
«правки должны работать на всех источниках».

Проверено живыми запросами 24.11.2026:

    GET https://plapi.cdnvideohub.com/api/v1/player/sv/playlist
        ?pub=745&aggr=mali&id=61967
        → {"titleName": "Чёрный клевер 2", "isSerial": true, "items": [48]}

    GET https://plapi.cdnvideohub.com/api/v1/player/sv/video/16355510147632
        → {"duration": 1374, "status": "STATUS_OK",
           "sources": {"hlsUrl": …, "mpegFullHdUrl": …, "mpegHighUrl": …}}

Ключ не нужен, браузер не нужен, cookies не нужны. Поток лежит на okcdn
(подпись привязана к IP и протухает) — поэтому резолв идёт в момент
открытия, а сегменты забирает наш мост свежими запросами.

**Где именно живёт плеер на сайте — это дело сайта, а не этого модуля.**
Модуль `yummyanime` сам находит на своей странице, какой `pub`/`aggr`/`id`
нужен, и присылает их сюда. Так новый сайт с тем же плеером подключается
одной функцией, а не копией разбора.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from ..core.cache import SizeCache, TTLCache, file_key
from ..core.errors import SourceFailedError
from ..core.models import Quality, SourceState, Stream, StreamKind, sort_qualities
from ..web.fetch import FetchError, fetch, json_get

API = "https://plapi.cdnvideohub.com/api/v1/player/sv"
"""Публичный API плеера. Проверено: без ключа, без cookies, без Referer."""

CACHE_TTL_S = 600
"""Плейлист живёт в кэше 10 минут: переключение озвучки — это повторный
resolve, и без кэша каждый клик тянул бы весь список серий. У «Ван-Писа»
это 615 КБ на нажатие."""

FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"

API_HEADERS = {
    "User-Agent": FIREFOX_UA,
    "Accept": "application/json, text/plain, */*",
}

MPEG_VARIANTS: tuple[tuple[str, int], ...] = (
    ("mpeg4kUrl", 2160),
    ("mpeg2kUrl", 1440),
    ("mpegQhdUrl", 1440),
    ("mpegFullHdUrl", 1080),
    ("mpegHighUrl", 720),
    ("mpegMediumUrl", 480),
    ("mpegLowUrl", 360),
    ("mpegLowestUrl", 240),
    ("mpegTinyUrl", 144),
)
"""Прямые mp4-варианты okcdn: имя поля → ПРЕДПОЛАГАЕМАЯ высота.

Высота из имени поля — только догадка: сайт называет её «high/medium/low»,
а не пикселями. Настоящий размер читается из самого файла (`tkhd` в moov),
и именно он попадает в качество. Если прочитать не удалось — вариант
не берём: качество с выдуманной высотой хуже, чем его отсутствие."""

_UNKNOWN_VOICE = "Неизвестный"
"""Заглушка сайта вместо названия студии. Показываем честно: «Без названия»."""

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CvhParams:
    """Что нужно плееру, чтобы отдать сериал. Ничего больше.

    `pub` и `aggr` — это «какой сайт спрашивает», `title_id` — «какой
    тайтл». У разных сайтов значения разные, а форма запроса одна.
    """

    pub: str
    aggr: str = ""
    title_id: str = ""

    def playlist_url(self) -> str:
        parts = [f"pub={self.pub}"]
        if self.aggr:
            parts.append(f"aggr={self.aggr}")
        parts.append(f"id={self.title_id}")
        return f"{API}/playlist?" + "&".join(parts)


MAX_PLAYLISTS = 8
"""Плейлист — это сотни килобайт (`Ван-Пис` — 615 КБ). Держим не больше
восьми: человек смотрит один сериал, редко два, а старое не пригодится —
TTL и так 10 минут."""

MAX_SIZES = 256
"""Размеры кадра — пара чисел на файл. Предел щедрый, но конечный: без
него словарь растёт с каждым новым сезоном и не чистится никогда."""

CACHE: TTLCache[dict[str, Any]] = TTLCache(limit=MAX_PLAYLISTS, ttl_s=CACHE_TTL_S)
"""Плейлисты по ключу «сайт + тайтл». Ограничены и по времени, и по числу:
раньше TTL проверялся только при чтении, и ключи, к которым не вернулись,
оседали в словаре навсегда."""

SIZES = SizeCache(limit=MAX_SIZES)
"""Размеры кадра по файлам. Срока жизни нет: разрешение файла не меняется."""


def voice_title(item: dict[str, Any]) -> str:
    """Подпись дорожки для пульта: студия (тип), без повторов.

    Сайт вместо студии иногда пишет «Неизвестный» — это не название, а
    заглушка. Тогда показываем тип: «Многоголосый», «Субтитры».
    """
    studio = str(item.get("voiceStudio") or "").strip()
    kind = str(item.get("voiceType") or "").strip()
    if not studio or studio == _UNKNOWN_VOICE:
        return kind or "Без названия"
    if kind and kind not in (_UNKNOWN_VOICE, "Неизвестный") and kind.lower() != studio.lower():
        return f"{studio} ({kind})"
    return studio


def tracks_by_episode(playlist: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    """Разложить дорожки по сериям, сохранив порядок сайта.

    Порядок внутри серии — как у сайта: он ставит основную озвучку первой.
    Свой порядок навязать нельзя: у Kodik есть штатный приоритет, а здесь
    единственный ориентир — то, что показывает сам сайт.
    """
    result: dict[int, list[dict[str, Any]]] = {}
    for item in playlist.get("items") or ():
        try:
            episode = int(item.get("episode") or 0)
        except (TypeError, ValueError):
            continue
        if not item.get("vkId"):
            continue
        result.setdefault(episode, []).append(item)
    return result


def pick(
    playlist: dict[str, Any], translation_index: int = 0, episode: int | None = None
) -> dict[str, Any]:
    """Дорожка для показа: серия, внутри неё — озвучка по номеру.

    Серия по умолчанию — первая (`min`), а не первая в списке: сайт может
    отдать плейлист с конца или с дырами в нумерации, и «первая строка»
    тогда окажется не первой серией.

    Если запрошенной серии нет, берём ближайшую меньшую, а если и её нет —
    первую. Так «5 серия» у тайтла из трёх не открывает пустоту молча:
    человек увидит хоть что-то и поймёт по подписи, какая серия на самом
    деле. Это отличается от Kodik, где такая просьба — честный отказ:
    здесь плеер не сообщает, сколько серий у каждой озвучки, и отказ
    выглядел бы загадкой.
    """
    groups = tracks_by_episode(playlist)
    if not groups:
        return {}
    wanted = int(episode or min(groups))
    if wanted in groups:
        chosen = wanted
    else:
        below = [number for number in groups if number < wanted]
        chosen = max(below) if below else min(groups)
    tracks = groups[chosen]
    index = translation_index if 0 <= translation_index < len(tracks) else 0
    return tracks[index]


def video_hls_url(video: dict[str, Any]) -> str:
    """Адрес HLS-мастера из ответа /video (пустая строка — нет такого)."""
    return str((video.get("sources") or {}).get("hlsUrl") or "")


BOX_HEADER = 8
"""Служебная шапка mp4-бокса: 4 байта размера + 4 байта имени."""

MAX_BOX_DEPTH = 6
"""Глубина вложенности, после которой перестаём искать `tkhd`."""


def _tkhd_size(head: bytes) -> tuple[int, int]:
    """Размер кадра из первых байт mp4 (`tkhd` внутри `moov`).

    Зачем разбирать руками: ffprobe на каждый вариант — это процесс и
    секунды, а размер лежит в открытом виде в первых килобайтах. Если
    структура нестандартная — вернём (0, 0) и вариант отбросим.
    """

    def walk(start: int, end: int, depth: int) -> tuple[int, int]:
        pos = start
        while pos + BOX_HEADER <= end and depth < MAX_BOX_DEPTH:
            size = int.from_bytes(head[pos : pos + 4], "big")
            kind = head[pos + 4 : pos + BOX_HEADER]
            if size < BOX_HEADER:
                return (0, 0)
            # Контейнер может быть ДЛИННЕЕ прочитанного куска: у okcdn `moov`
            # весит 1,1 МБ, а мы читаем 256 КБ. `tkhd` лежит в самом начале
            # `moov`, поэтому спускаемся в обрезанный контейнер, а не
            # сдаёмся — иначе 1080p не попадёт в список вовсе.
            body = pos + BOX_HEADER
            stop = min(pos + size, end)
            if kind in (b"moov", b"trak"):
                found = walk(body, stop, depth + 1)
                if found[1]:
                    return found
            elif kind == b"tkhd":
                version = head[body] if body < len(head) else 0
                offset = body + (88 if version == 1 else 76)
                width = int.from_bytes(head[offset : offset + 4], "big") >> 16
                height = int.from_bytes(head[offset + 4 : offset + 8], "big") >> 16
                return (width, height)
            pos += size
        return (0, 0)

    return walk(0, len(head), 0)


def probe_mp4_size(url: str) -> tuple[int, int]:
    """Размер кадра файла: GET первых 256 КБ. (0, 0) — прочитать не вышло."""
    try:
        head = fetch(url, {**API_HEADERS, "Range": "bytes=0-262143"}).body
    except FetchError:
        return (0, 0)
    return _tkhd_size(head)


def qualities_from_video(video: dict[str, Any]) -> tuple[Quality, ...]:
    """Качества из ответа /video: прямые mp4 с проверенным размером.

    Почему mp4, а не HLS: `hlsUrl` у okcdn отдаёт мастер, внутри которого
    ссылки ВИДА `/expires/…/type/2/sig/…`, а не абсолютные адреса. Наш мост
    переписывает плейлист относительно `final_url` мастера, и такие
    «пути» превращаются в мусор — hls.js получает 404. Проверено: сам
    мастер отдаётся (200), а качество по нему не играет.

    Прямой mp4 работает без плейлистов и подписан на тот же IP, что и мост,
    поэтому путь надёжнее. Минус честный: переключение качества — это смена
    файла, а не уровня в одном потоке; hls.js тут не нужен вовсе.
    """
    sources = video.get("sources") or {}
    wanted = [(field, str(sources.get(field) or "")) for field, _ in MPEG_VARIANTS]
    wanted = [(field, url) for field, url in wanted if url]
    if not wanted:
        return ()

    # Пробы идут ОДНОВРЕМЕННО: шесть последовательных GET по 256 КБ давали
    # 33 секунды перед стартом (замерено), параллельно — около 8.
    def probe(item: tuple[str, str]) -> tuple[str, str, tuple[int, int]]:
        field, url = item
        key = file_key(field, url)
        known = SIZES.get(key)
        if known is not None:
            return field, url, known
        size = probe_mp4_size(url)
        if size[1] > 0:
            SIZES.put(key, size)
        return field, url, size

    with ThreadPoolExecutor(max_workers=len(wanted)) as pool:
        results = list(pool.map(probe, wanted))

    found: list[Quality] = []
    for _field, url, (width, height) in results:
        if height <= 0:
            continue
        # Битрейт файла не сообщается: оценка по площади кадра — нижняя
        # граница H.264, чтобы плеер не выбирал вслепую.
        bitrate = max(1, round(width * height * 25 / 10_000))
        found.append(Quality(height=height, width=width, bitrate_kbps=bitrate, url=url))
    return sort_qualities(found)


def parse_master(playlist: str) -> tuple[Quality, ...]:
    """Master-плейлист okcdn → качества с настоящими размерами и битрейтом.

    Оставлен как запасной путь: используется, только если прямых mp4 в
    ответе API не оказалось.
    """
    items: list[Quality] = []
    for block in re.finditer(
        r"#EXT-X-STREAM-INF:([^\n]+)\n([^\n#]+)", playlist or "", re.IGNORECASE
    ):
        attrs, link = block.group(1), block.group(2).strip()
        resolution = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs, re.IGNORECASE)
        if not resolution or not link.startswith("http"):
            continue
        width, height = int(resolution.group(1)), int(resolution.group(2))
        bandwidth = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", attrs, re.IGNORECASE)
        kbps = round(int(bandwidth.group(1)) / 1000) if bandwidth else 0
        if kbps <= 0:
            kbps = max(1, round(width * height * 25 / 10_000))
        items.append(Quality(height=height, width=width, bitrate_kbps=kbps, url=link))
    return sort_qualities(items)


def fetch_master(url: str) -> tuple[Quality, ...]:
    """Запасной путь: разобрать качества из HLS-мастера okcdn."""
    if not url:
        return ()
    try:
        text = fetch(url, API_HEADERS).text()
    except FetchError as exc:
        raise SourceFailedError(
            "cdnvideohub",
            f"плейлист качеств не отдался: {exc.reason}",
            fix="Подпись VK протухла — откройте ссылку заново",
            state=SourceState.PARTIAL,
        ) from exc
    return parse_master(text)


def fetch_playlist(params: CvhParams, cache_key: str) -> dict[str, Any]:
    """Плейлист сериала: из кэша или запросом.

    Кэш по ключу «сайт + тайтл», а не по ссылке: один и тот же сериал
    открывают с разными хвостами адреса, а плейлист у него один.
    """
    cached = CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        data = json_get(params.playlist_url(), API_HEADERS)
    except FetchError as exc:
        raise SourceFailedError(
            "cdnvideohub",
            f"плейлист не отдался: {exc.reason}",
            fix="API плеера мог переехать — пришлите ссылку разработчику",
            state=SourceState.PARTIAL,
        ) from exc
    if not isinstance(data, dict) or not data.get("items"):
        raise SourceFailedError(
            "cdnvideohub",
            "плейлист пуст или не разобрался",
            fix="API вернул неожиданный ответ — пришлите ссылку разработчику",
            state=SourceState.BROKEN,
        )
    CACHE.put(cache_key, data)
    return data


def fetch_video(vk_id: str) -> dict[str, Any]:
    """Ссылка на поток по video id. Подпись живёт минуты, не кэшируем."""
    try:
        data = json_get(f"{API}/video/{vk_id}", API_HEADERS)
    except FetchError as exc:
        raise SourceFailedError(
            "cdnvideohub",
            f"поток не отдался: {exc.reason}",
            fix="VK мог удалить видео — проверьте в браузере",
            state=SourceState.PARTIAL,
        ) from exc
    if not isinstance(data, dict):
        raise SourceFailedError(
            "cdnvideohub",
            "поток вернулся не объектом",
            fix="API вернул неожиданный ответ — пришлите ссылку разработчику",
            state=SourceState.BROKEN,
        )
    return data


def build_stream(
    *,
    source_name: str,
    site: str,
    params: CvhParams,
    base_title: str,
    translation_index: int = 0,
    episode: int | None = None,
    cache_key: str = "",
    stream_headers: dict[str, str] | None = None,
) -> Stream:
    """Сайт уже нашёл параметры плеера → общий разбор → поток.

    Единственное место, где CVH превращается в `Stream`. Сайт приносит
    только то, что знает сам: `pub`, `aggr`, `title_id` и название.
    """
    playlist = fetch_playlist(params, cache_key or f"{site}:{params.title_id}")
    chosen = pick(playlist, translation_index, episode)
    if not chosen:
        raise SourceFailedError(
            source_name,
            "в плейлисте нет ни одной дорожки",
            fix="Возможно, у сериала ещё нет видео — проверьте в браузере",
            state=SourceState.PARTIAL,
        )

    video = fetch_video(str(chosen["vkId"]))
    qualities = qualities_from_video(video)
    if not qualities:
        # запасной путь: если okcdn перестанет держать mp4 — HLS
        log.warning("%s: прямых mp4 нет, пробуем HLS-мастер", source_name)
        qualities = fetch_master(video_hls_url(video))
    if not qualities:
        raise SourceFailedError(
            source_name,
            "плеер не отдал адрес потока",
            fix="Файл мог быть удалён с VK — проверьте в браузере",
            state=SourceState.PARTIAL,
        )

    groups = tracks_by_episode(playlist)
    current_episode = int(chosen.get("episode") or 1)
    tracks = groups.get(current_episode, [])
    # Серии наружу — по общему контракту `Stream.episodes`, как у Kodik.
    # Сезонов CVH не сообщает: серии идут одним списком.
    episode_list = tuple(
        {"season": "", "episode": str(number), "title": f"{number} серия"}
        for number in sorted(groups)
    )
    title = str(playlist.get("titleName") or base_title or site)
    log.info(
        "%s: %s → серия %s, %s, %s варианта",
        source_name,
        site,
        current_episode,
        voice_title(chosen),
        len(qualities),
    )
    return Stream(
        source=source_name,
        title=f"{title} — серия {current_episode}",
        kind=StreamKind.HLS,
        qualities=qualities,
        duration_s=int(video.get("duration") or 0),
        # Качества — прямые mp4 (см. qualities_from_video): hls.js их
        # не разбирает, плеер переключает src сам.
        is_direct_file=True,
        # Озвучки едут списком: пульт рисует выпадающий список, а
        # переключение — повторный resolve с другим индексом, как у Kodik.
        audio_tracks=tuple(
            {
                "id": str(item.get("cvhId") or item.get("vkId") or ""),
                "title": voice_title(item),
                "type": str(item.get("voiceType") or ""),
                "episode": str(item.get("episode") or ""),
            }
            for item in tracks
        ),
        episodes=episode_list,
        episode=current_episode,
        can_switch_episode=len(episode_list) > 1,
        state=SourceState.OK,
        # Подпись okcdn привязана к IP. Замер громкости идёт мимо моста,
        # поэтому заголовки обязаны ехать с потоком.
        headers=stream_headers
        or {
            "User-Agent": FIREFOX_UA,
            "Referer": f"https://{site}/",
        },
    )


__all__ = [
    "API",
    "CACHE",
    "MPEG_VARIANTS",
    "SIZES",
    "CvhParams",
    "build_stream",
    "fetch_master",
    "fetch_playlist",
    "fetch_video",
    "parse_master",
    "pick",
    "probe_mp4_size",
    "qualities_from_video",
    "tracks_by_episode",
    "video_hls_url",
    "voice_title",
]
