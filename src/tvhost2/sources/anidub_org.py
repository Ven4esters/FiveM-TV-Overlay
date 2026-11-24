"""anidub.org — сайт, у которого плеер cdnvideohub (`cvh`).

История: раньше anidub.com отдавал серию через плеер на `ladonyvesna2005.info`,
и разбор был двухступенчатым (iframe → `data-stream-src` → `vid.php`). Этот
путь жив до сих пор и обслуживается модулем `anidub.py`.

Домен **anidub.org** встраивает веб-компонент `<video-player>`:

    GET https://anidub.org/6093-van-pis-tv.html
        → <video-player data-publisher-id="19" data-aggregator="cvh"
                        data-title-id="019169b6-9f49-7086-a8d3-76f1e2558ffa">

Сам плеер — общий и живёт в `cvh.py`: тот же API стоит на yummyanime,
отличаются только `pub` и `aggr`. Здесь остаётся только то, что знает
про себя anidub.org: где на странице компонент и какие у него числа.

Проверено живыми запросами 24.11.2026: плейлист 615 КБ у «Ван-Писа»
(3759 дорожек на 1180 серий), поток на okcdn (VK-видео, подпись по IP).

Про серии: сайт не даёт «серия N» отдельным адресом — серия и озвучка
выбираются внутри плеера, а страница одна. Плейлист перечисляет дорожки в
порядке серий: для каждой серии идут её озвучки. Поэтому серия по
умолчанию — первая, а озвучки для списка — те, что есть у этой серии.
Так «Ван-Пис» открывается с 1-й серии в дубляже AniLiberty, а не с 1180-й
в случайной озвучке — проверено на живом плейлисте.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from typing import Any

from ..core.errors import SourceFailedError
from ..core.models import SourceState, Stream, StreamKind
from ..web.fetch import FetchError, fetch
from . import cvh

SITE = "anidub.org"
"""Домен, который обслуживает ЭТОТ модуль. anidub.com — соседний модуль."""

# Имена, которые раньше жили в этом модуле, а теперь в общем `cvh`.
# Оставлены ссылками: снаружи их импортируют тесты и модуль yummy, и
# ломать этот импорт ради переезда кода незачем.
API = cvh.API
API_HEADERS = cvh.API_HEADERS
FIREFOX_UA = cvh.FIREFOX_UA
CACHE = cvh.CACHE
SIZES = cvh.SIZES
CACHE_TTL_S = cvh.CACHE_TTL_S
MPEG_VARIANTS = cvh.MPEG_VARIANTS
voice_title = cvh.voice_title
tracks_by_episode = cvh.tracks_by_episode
pick = cvh.pick
fetch_master = cvh.fetch_master
parse_master = cvh.parse_master
qualities_from_video = cvh.qualities_from_video
probe_mp4_size = cvh.probe_mp4_size
video_hls_url = cvh.video_hls_url
_tkhd_size = cvh._tkhd_size

_PAGE_RE = re.compile(r"^https?://(?:www\.)?anidub\.org/", re.IGNORECASE)

_PLAYER_RE = re.compile(r"<video-player\b([^>]*)>", re.IGNORECASE | re.DOTALL)
"""Веб-компонент на странице. Атрибуты вытаскиваем отдельно — порядок
не гарантирован, а `>` внутри атрибутов нам не встречается."""

_ATTR_RE = re.compile(r'data-(publisher-id|aggregator|title-id)="([^"]+)"', re.IGNORECASE)

_TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.IGNORECASE)

TITLE_SUFFIXES = (" смотреть аниме", " смотреть онлайн", " смотреть")
"""Хвосты в <title>. Обрезаем первый найденный — иначе в оверлее
показывается «Ван-Пис [ТВ] смотреть аниме онлайн бесплатно на сайте»."""

log = logging.getLogger(__name__)


def is_anidub_org(url: str) -> bool:
    """Ссылка ведёт на anidub.org (новый плеер)?"""
    return bool(_PAGE_RE.match(url or ""))


def player_params(html: str) -> dict[str, str]:
    """Параметры веб-компонента со страницы: pub, aggr, id.

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
    return cvh.CvhParams(
        pub=params["pub"], aggr=params.get("aggr", ""), title_id=params["id"]
    ).playlist_url()


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

        Всё, что ниже плеера (плейлист → /video → mp4-варианты → `Stream`),
        делает общий `cvh`: этот же плеер стоит на yummyanime, и разбор
        там должен быть ровно такой же.
        """
        page = self._page(url)
        found = player_params(page)
        if not found:
            raise SourceFailedError(
                self.name,
                "на странице нет плеера",
                fix="Нужна страница сериала: anidub.org/<id>-<название>.html",
                state=SourceState.PARTIAL,
            )

        stream = cvh.build_stream(
            source_name=self.name,
            site=SITE,
            params=cvh.CvhParams(
                pub=found["pub"], aggr=found.get("aggr", ""), title_id=found["id"]
            ),
            base_title=clean_title(page) or SITE,
            translation_index=translation_index,
            episode=episode,
            cache_key=f"{SITE}:{found['id']}",
            # Подпись okcdn привязана к IP. Замер громкости идёт мимо моста,
            # поэтому заголовки обязаны ехать с потоком.
            stream_headers={
                "User-Agent": FIREFOX_UA,
                "Referer": f"https://{SITE}/",
            },
        )
        _log_info(f"anidub.org: {url} → серия {stream.episode}, {len(stream.qualities)} варианта")
        return stream

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
