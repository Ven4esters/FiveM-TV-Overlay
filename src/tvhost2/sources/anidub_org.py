"""anidub.org — новый плеер cdnvideohub (aggregator `cvh`).

История: раньше anidub.com отдавал серию через плеер на `ladonyvesna2005.info`,
и разбор был двухступенчатым (iframe → `data-stream-src` → `vid.php`). Этот
путь жив до сих пор и обслуживается модулем `anidub.py`.

Домен **anidub.org** устроен иначе: страница встраивает веб-компонент
`<video-player>` с идентификаторами, а сам плеер ходит в открытый JSON API.
Проверено живыми запросами 24.11.2026:

    GET https://anidub.org/6093-van-pis-tv.html
        → <video-player data-publisher-id="19" data-aggregator="cvh"
                        data-title-id="019169b6-9f49-7086-a8d3-76f1e2558ffa">

    GET https://plapi.cdnvideohub.com/api/v1/player/sv/playlist
        ?pub=19&aggr=cvh&id=<title-id>
        → 615 КБ JSON: {"titleName": "Ван-Пис", "isSerial": true, "items": [...]}

    GET https://plapi.cdnvideohub.com/api/v1/player/sv/video/<vkId>
        → {"duration": 1416, "status": "STATUS_OK",
           "sources": {"hlsUrl": "https://vd538.okcdn.ru/video.m3u8?…"}}

Три вещи, которые здесь важнее кода:

1. **Ключ не нужен, браузер не нужен.** Анонимный GET отдаёт всё: ни Anubis,
   ни cookies, ни подписи запроса. Проверено curl'ом с чистого IP.

2. **Поток лежит на okcdn.ru — это VK-видео.** Тот же CDN, что у источника
   VK, и та же беда: подпись привязана к IP (`srcIp=`) и протухает. Значит
   резолв делаем в момент открытия, а сегменты забирает наш мост свежими
   запросами — как и для остальных источников.

3. **Плейлист — 615 КБ и почти мегабайт по времени разбора.** У «Ван-Писа»
   3759 дорожек на 1180 серий. Дёргать его на каждое переключение озвучки
   (а пульт переключает озвучку повторным resolve) — это 615 КБ на клик.
   Поэтому плейлист кэшируется по `title-id` на 10 минут: сериал за это
   время не меняется, а сайт не получает лишних запросов.

Про серии: сайт не даёт «серия N» отдельным адресом — серия и озвучка
выбираются внутри плеера, а страница одна. Плейлист перечисляет дорожки в
порядке серий: для каждой серии идут её озвучки. Поэтому:

  - серия по умолчанию — первая, у которой есть выбранная озвучка;
  - озвучки для списка — те, что есть у этой серии, в порядке плейлиста.

Так «Ван-Пис» открывается с 1-й серии в дубляже AniLiberty, а не с 1180-й
в случайной озвучке — проверено на живом плейлисте.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..core.errors import SourceFailedError
from ..core.models import Quality, SourceState, Stream, StreamKind, sort_qualities
from ..web.fetch import FetchError, fetch, json_get

SITE = "anidub.org"
"""Домен, который обслуживает ЭТОТ модуль. anidub.com — соседний модуль."""

API = "https://plapi.cdnvideohub.com/api/v1/player/sv"
"""Публичный API плеера. Проверено: без ключа, без cookies, без Referer."""

_PAGE_RE = re.compile(r"^https?://(?:www\.)?anidub\.org/", re.IGNORECASE)

_PLAYER_RE = re.compile(r"<video-player\b([^>]*)>", re.IGNORECASE | re.DOTALL)
"""Веб-компонент на странице. Атрибуты вытаскиваем отдельно — порядок
не гарантирован, а `>` внутри атрибутов нам не встречается."""

_ATTR_RE = re.compile(r'data-(publisher-id|aggregator|title-id)="([^"]+)"', re.IGNORECASE)

_TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.IGNORECASE)

TITLE_SUFFIXES = (" смотреть аниме", " смотреть онлайн", " смотреть")
"""Хвосты в <title>. Обрезаем первый найденный — иначе в оверлее
показывается «Ван-Пис [ТВ] смотреть аниме онлайн бесплатно на сайте»."""

CACHE_TTL_S = 600
"""Плейлист на 615 КБ живёт в кэше 10 минут: переключение озвучки это
повторный resolve, и без кэша каждый клик тянул бы весь список серий."""

log = logging.getLogger(__name__)

_UNKNOWN_VOICE = "Неизвестный"
"""Заглушка сайта вместо названия студии. Показываем честно: «Без названия»."""


class _PlaylistCache:
    """Кэш плейлистов по title-id. Потокобезопасный: прокси многопоточный."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def get(self, title_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(title_id)
            if item is None:
                return None
            at, data = item
            if time.time() - at > CACHE_TTL_S:
                del self._items[title_id]
                return None
            return data

    def put(self, title_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._items[title_id] = (time.time(), data)


CACHE = _PlaylistCache()


class _SizeCache:
    """Размеры кадра по файлам: имя поля + префикс адреса.

    Зачем: проба — это GET на 256 КБ с каждого варианта, и шесть таких
    подряд дают ощутимую паузу перед стартом. 1080p-файл у сериала один и
    тот же, сколько раз его ни открывай, поэтому результат запоминается.
    Ключ — имя поля и id файла из адреса: подпись в нём меняется, а id нет.
    """

    def __init__(self) -> None:
        self._items: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(field: str, url: str) -> str:
        match = re.search(r"/id/(\d+)/", url)
        file_id = match.group(1) if match else url.split("?", maxsplit=1)[0]
        return f"{field}:{file_id}"

    def get(self, key: str) -> tuple[int, int] | None:
        with self._lock:
            return self._items.get(key)

    def put(self, key: str, size: tuple[int, int]) -> None:
        with self._lock:
            self._items[key] = size


SIZES = _SizeCache()


def is_anidub_org(url: str) -> bool:
    """Ссылка ведёт на anidub.org (новый плеер)?"""
    return bool(_PAGE_RE.match(url or ""))


def player_params(html: str) -> dict[str, str]:
    """Параметры веб-компонента со страницы: pub, aggr, id, title.

    Пустой словарь — компонента на странице нет (видео удалено, съехала
    вёрстка). Пустые значения отсеиваются сразу: API без `id` отвечает
    мусором, и лучше сказать «не нашёл плеер», чем разбирать мусор.
    """
    match = _PLAYER_RE.search(html or "")
    if not match:
        return {}
    found = {key.lower(): value.strip() for key, value in _ATTR_RE.findall(match.group(1))}
    params = {
        "pub": found.get("publisher-id", ""),
        "aggr": found.get("aggregator", ""),
        "id": found.get("title-id", ""),
    }
    if not params["id"] or not params["pub"]:
        return {}
    return params


def clean_title(html: str) -> str:
    """Название из <title> без рекламных хвостов."""
    match = _TITLE_RE.search(html or "")
    if not match:
        return ""
    # HTML-сущности обязательны: без разэкранирования в оверлее видно
    # «Ван-Пис &#91;ТВ&#93;» вместо «Ван-Пис [ТВ]» — проверено на живой странице.
    title = html_lib.unescape(match.group(1))
    title = re.sub(r"\s+", " ", title).strip()
    low = title.lower()
    for suffix in TITLE_SUFFIXES:
        cut = low.find(suffix)
        if cut > 0:
            title = title[:cut].strip()
            low = title.lower()
    return title


def playlist_url(params: dict[str, str]) -> str:
    """Адрес плейлиста. `aggr` не обязателен, но сайт всегда его шлёт."""
    parts = [f"pub={params['pub']}"]
    if params.get("aggr"):
        parts.append(f"aggr={params['aggr']}")
    parts.append(f"id={params['id']}")
    return f"{API}/playlist?" + "&".join(parts)


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
    единственный ориентир — то, что показывает сам anidub.
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
    """Выбрать дорожку: серия 1 (или первая доступная) + озвучка по индексу.

    Почему первая серия, а не последняя: на странице сериала сайт сам
    открывает серию 1, и человек ждёт того же. (Проверено: Chrome на
    .org/6093-van-pis-tv.html запрашивает /video/16241962670640 — это
    серия 1180, то есть сайт продолжает с последней. Но у зрителя, который
    впервые открывает сериал, «продолжить с 1180-й» — не то, что нужно;
    первая серия честнее и предсказуемее. Возврат к прошлому видео
    появится отдельной задачей.)

    Пустой список — ни одной дорожки с video id.
    """
    groups = tracks_by_episode(playlist)
    if not groups:
        return {}
    # Серия, если её просили и она есть; иначе первая доступная. Проверка
    # на «есть» обязательна: у сериала могут быть дыры в нумерации, и
    # запрос несуществующей серии дал бы пустоту вместо понятной ошибки.
    wanted = episode if episode and episode in groups else min(groups)
    tracks = groups[wanted]
    index = max(0, min(translation_index, len(tracks) - 1))
    return tracks[index]


FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"
"""Один и тот же UA на ВСЕ запросы к API и к CDN.

Почему нельзя по-разному: API выдаёт подпись под тот браузер, которым
пришёл запрос, и пишет это в саму ссылку (`srcAg=GECKO` / `srcAg=CHROME`).
CDN проверяет соответствие и отвечает 400 тому, кто не совпал. Проверено:
одна и та же ссылка с GECKO-подписью отдала 206 на Firefox-UA и 400 на
Chrome-UA. Наш `fetch()` по умолчанию ставит Chrome для okcdn.ru — поэтому
заголовки передаём явно ВЕЗДЕ, включая мост (через Stream.headers).
"""

API_HEADERS = {"User-Agent": FIREFOX_UA, "Accept": "application/json"}

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
не берём: качество с выдуманной высотой хуже, чем его отсутствие.
"""

ATOM_HEADER_BYTES = 8
"""Любой атом mp4: 4 байта размера + 4 байта имени."""

TKHD_MIN_BYTES = 92
"""Минимальный tkhd (версия 0): меньше — структура битая или не та."""

_TREE_DEPTH = 4
"""Глубина спуска по атомам: moov → trak → … глубже не бывает."""

_MOOV_READ_BYTES = 256 * 1024
"""Сколько читаем с начала mp4, чтобы найти tkhd.

Проверено на живом файле okcdn: `ftyp (32 байта) → moov (1,5 МБ)`, а
`tkhd` лежит в НАЧАЛЕ moov (смещение ~160 байт от начала файла). Обход
больше не требует, чтобы контейнеры влезали целиком, поэтому хватает
256 КБ — а это два-три коротких GET на резолв вместо шести по 3 МБ.

Обычный mp4 от ffmpeg может держать moov в конце — тогда размер честно
не прочитается, вариант не попадёт в список, и плеер возьмёт HLS.
"""


def video_hls_url(video: dict[str, Any]) -> str:
    """Адрес master-плейлиста из ответа /video."""
    return str((video.get("sources") or {}).get("hlsUrl") or "")


def _tkhd_size(head: bytes) -> tuple[int, int]:
    """Ширина и высота из первого tkhd в прочитанном начале файла.

    tkhd лежит внутри moov → trak; идём по атомам, а не ищем байты: подпись
    `tkhd` может случайно встретиться в данных, а размер атома — нет.
    Возвращает (0, 0), если не нашли или формат не тот.
    """

    def walk(start: int, end: int, depth: int) -> tuple[int, int]:  # noqa: PLR0911
        """Рекурсивный обход атомов.

        Возвратов много намеренно: каждый — свой случай формата (глубина,
        пустой список, битый размер, tkhd найден). Свести их в один флаг
        значит спрятать причину, а причина нужна в журнале.
        """
        if depth > _TREE_DEPTH:
            return 0, 0
        index = start
        while index + ATOM_HEADER_BYTES <= end:
            size = int.from_bytes(head[index : index + 4], "big")
            kind = head[index + 4 : index + 8]
            if size < ATOM_HEADER_BYTES:
                return 0, 0
            if kind in (b"moov", b"trak"):
                # Заходим внутрь, ДАЖЕ если контейнер не влез в буфер целиком:
                # нужные нам tkhd/mvhd лежат в его начале. У okcdn `trak`
                # весит 646 КБ (таблицы кадров), и требование «контейнер
                # должен поместиться» давало размер 0 при буфере 512 КБ —
                # проверено, это и ломало сбор качеств.
                found = walk(index + ATOM_HEADER_BYTES, min(index + size, end), depth + 1)
                if found != (0, 0):
                    return found
            elif kind == b"tkhd":
                # tkhd обязан поместиться: его ширина/высота — последние 8 байт
                if size >= TKHD_MIN_BYTES and index + size <= end:
                    width = int.from_bytes(head[index + size - 8 : index + size - 4], "big")
                    height = int.from_bytes(head[index + size - 4 : index + size], "big")
                    return width >> 16, height >> 16
                return 0, 0
            if index + size > end:
                # листовой атом не влез в буфер — дальше идти некуда
                return 0, 0
            index += size
        return 0, 0

    return walk(0, len(head), 0)


def probe_mp4_size(url: str) -> tuple[int, int]:
    """Прочитать настоящий размер кадра из прямого mp4 (два коротких GET).

    Зачем вообще: у okcdn `mpegHighUrl` и `mpegFullHdUrl` — разные файлы,
    но названия не говорят, какие там пиксели. Выдумывать «high = 720p»
    значит однажды показать в пульте 720p, а отдать 1080p.
    """
    try:
        head = fetch(url, API_HEADERS, max_bytes=_MOOV_READ_BYTES).body
    except FetchError:
        return 0, 0
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
        key = SIZES.key(field, url)
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
            "anidub.org",
            f"плейлист качеств не отдался: {exc.reason}",
            fix="Подпись VK протухла — откройте ссылку заново",
            state=SourceState.PARTIAL,
        ) from exc
    return parse_master(text)


class AnidubOrgSource:
    """anidub.org: страница → плейлист API → /video → master okcdn → HLS."""

    name = "anidub.org"
    state = SourceState.OK
    note = "аниме, новый плеер, до 1080p, без браузера"
    kind = StreamKind.HLS

    def matches(self, url: str) -> bool:
        return is_anidub_org(url)

    def resolve(self, url: str, translation_index: int = 0, episode: int | None = None) -> Stream:
        """Ссылка на страницу → поток. Ступени проверяются по одной.

        Ступень, которая сломалась, видна в журнале по тексту причины:
        «страница не открылась», «на странице нет плеера», «плейлист не
        отдался», «нет ни одной дорожки». Иначе при жалобе «не работает»
        непонятно, что именно менять.
        """
        page = self._page(url)
        params = player_params(page)
        if not params:
            raise SourceFailedError(
                self.name,
                "на странице нет плеера",
                fix="Нужна страница сериала: anidub.org/<id>-<название>.html",
                state=SourceState.PARTIAL,
            )

        playlist = self._playlist(params)
        chosen = pick(playlist, translation_index, episode)
        if not chosen:
            raise SourceFailedError(
                self.name,
                "в плейлисте нет ни одной дорожки",
                fix="Возможно, у сериала ещё нет видео — проверьте в браузере",
                state=SourceState.PARTIAL,
            )

        video = self._video(str(chosen["vkId"]))
        qualities = qualities_from_video(video)
        if not qualities:
            # запасной путь: если okcdn перестанет держать mp4 — HLS
            log.warning("anidub.org: прямых mp4 нет, пробуем HLS-мастер")
            qualities = fetch_master(video_hls_url(video))
        if not qualities:
            raise SourceFailedError(
                self.name,
                "плеер не отдал адрес потока",
                fix="Файл мог быть удалён с VK — проверьте в браузере",
                state=SourceState.PARTIAL,
            )

        groups = tracks_by_episode(playlist)
        tracks = groups.get(int(chosen.get("episode") or 1), [])
        current_episode = int(chosen.get("episode") or 1)
        # Серии наружу — по общему контракту `Stream.episodes`, как у Kodik.
        # У anidub.org сезонов нет вовсе: серии идут одним списком.
        episode_list = tuple(
            {"season": "", "episode": str(number), "title": f"{number} серия"}
            for number in sorted(groups)
        )
        base_title = str(playlist.get("titleName") or clean_title(page) or "anidub")
        log_line = (
            f"anidub.org: {url} → серия {current_episode}, "
            f"{voice_title(chosen)}, {len(qualities)} варианта"
        )
        _log_info(log_line)
        return Stream(
            source=self.name,
            title=f"{base_title} — серия {current_episode}",
            kind=self.kind,
            qualities=qualities,
            duration_s=int(video.get("duration") or 0),
            # Качества — прямые mp4 (см. qualities_from_video): hls.js их не
            # разбирает, плеер переключает src сам.
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
            state=self.state,
            # Подпись okcdn привязана к IP. Замер громкости идёт мимо моста,
            # поэтому заголовки обязаны ехать с потоком; for okcdn.ru fetch()
            # и так ставит Chrome-UA, но Referer задаём явно.
            headers={
                "User-Agent": FIREFOX_UA,
                "Referer": f"https://{SITE}/",
            },
        )

    # ---------------- ступени ----------------

    def _page(self, url: str) -> str:
        try:
            return fetch(url, API_HEADERS).text()
        except FetchError as exc:
            raise SourceFailedError(
                self.name,
                f"страница не открылась: {exc.reason}",
                fix="Проверьте ссылку и интернет",
                state=SourceState.BROKEN,
            ) from exc

    def _playlist(self, params: dict[str, str]) -> dict[str, Any]:
        """Плейлист сериала: из кэша или запросом.

        Кэш по title-id, а не по ссылке: один и тот же сериал открывают с
        разными хвостами адреса, а плейлист у него один.
        """
        cached = CACHE.get(params["id"])
        if cached is not None:
            return cached
        try:
            data = json_get(playlist_url(params), API_HEADERS)
        except FetchError as exc:
            raise SourceFailedError(
                self.name,
                f"плейлист не отдался: {exc.reason}",
                fix="API плеера мог переехать — пришлите ссылку разработчику",
                state=SourceState.PARTIAL,
            ) from exc
        if not isinstance(data, dict) or not data.get("items"):
            raise SourceFailedError(
                self.name,
                "плейлист пуст или не разобрался",
                fix="API вернул неожиданный ответ — пришлите ссылку разработчику",
                state=SourceState.BROKEN,
            )
        CACHE.put(params["id"], data)
        return data

    def _video(self, vk_id: str) -> dict[str, Any]:
        """Ссылка на поток по video id. Подпись живёт минуты, не кэшируем."""
        try:
            data = json_get(f"{API}/video/{vk_id}", API_HEADERS)
        except FetchError as exc:
            raise SourceFailedError(
                self.name,
                f"поток не отдался: {exc.reason}",
                fix="VK мог удалить видео — проверьте в браузере",
                state=SourceState.PARTIAL,
            ) from exc
        if not isinstance(data, dict):
            raise SourceFailedError(
                self.name,
                "поток вернулся не объектом",
                fix="API вернул неожиданный ответ — пришлите ссылку разработчику",
                state=SourceState.BROKEN,
            )
        return data


def _log_info(line: str) -> None:
    log.info(line)


def debug_dump(html: str) -> dict[str, Any]:
    """Что мы вообще получили — для журнала при разборе жалоб."""
    return {
        "length": len(html or ""),
        "players": len(_PLAYER_RE.findall(html or "")),
        "params": sorted(player_params(html)),
        "title": clean_title(html),
    }


__all__ = [
    "CACHE",
    "MPEG_VARIANTS",
    "AnidubOrgSource",
    "clean_title",
    "debug_dump",
    "fetch_master",
    "is_anidub_org",
    "parse_master",
    "pick",
    "player_params",
    "playlist_url",
    "probe_mp4_size",
    "qualities_from_video",
    "tracks_by_episode",
    "video_hls_url",
    "voice_title",
]
