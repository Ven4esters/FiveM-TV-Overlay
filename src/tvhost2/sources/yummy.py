"""YummyAnime (old.yummyani.me) — каталог с открытым API вместо плеера в HTML.

Особенность сайта, которая и определила устройство модуля: в HTML нет ни
плеера, ни ссылки на видео. Страница — пустой каркас, а данные приезжают
запросом из скрипта:

    GET https://old.yummyani.me/api/anime/<page_id>/videos
    → {"response": [{"video_id":588089, "number":1, "duration":1443,
                     "data": {"player":"Плеер Kodik", "dubbing":"Озвучка JAM"},
                     "iframe_url":"//kodikplayer.com/season/92734/<hash>/720p?…"}]}

`page_id` лежит в самой странице: `<meta id="page_id" content="10618">`.
Значит, разбор такой: страница → page_id → JSON → iframe → плеер.

На одном тайтле плееры смешаны, и это важно для честности:

  - **Kodik** — работает, его и разбираем (у «Блича» 13 серий × 27 озвучек);
  - **CVH** (cdnvideohub) — у нас уже есть для anidub.org, но здесь ссылки
    идут на промежуточную страницу `ru.yummyani.me/iframeCVH.html`, и её
    формат отличается: это не тот прямой `<video-player>`, что на anidub;
  - **Alloha** — отвечает прямым текстом, который заказчик просил не
    замалчивать: «Извините, но данный контент недоступен в вашем регионе».

Поэтому модуль не «поддерживает yummyani», а поддерживает то, что на
yummyani реально отдаётся плеером Kodik. Остальное — честный отказ с
объяснением, а не молчаливая пустота.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from ..core.errors import SourceFailedError
from ..core.models import SourceState, Stream
from ..web.fetch import FetchError, fetch_text
from .kodik import KodikSource
from .kodik_embed import page_title

HOSTS = ("yummyani.me", "yani.tv", "yummy-anime.ru")
"""Домены каталога. Проверяем по окончанию, чтобы `old.` и `ru.` попадали."""

_PAGE_ID_RE = re.compile(r'id="page_id"[^>]*content="(\d+)"')
"""Номер тайтла: `<meta id="page_id" name="page_id" content="10618">`."""

_NAME = "YummyAnime"

DEFAULT_SEASON = 1
"""Сезон по умолчанию. У сайта сезон выбирается отдельно, а мы открываем
первый: в 9 из 10 случаев человек смотрит с начала."""


def players_present(data: Any) -> set[str]:
    """Короткие имена плееров, которые сайт отдал для этого тайтла.

    `"Плеер Kodik"` → `"Kodik"`: длинное название нужно сайту, а нам — для
    фразы «доступно только через Alloha».
    """
    items = (data or {}).get("response") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return set()
    names: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        player = str((item.get("data") or {}).get("player") or "")
        word = player.replace("Плеер", "").strip()
        if word:
            names.add(word)
    return names


def is_yummy(url: str) -> bool:
    """Ссылка на страницу каталога YummyAnime."""
    host = urllib.parse.urlsplit(url).netloc.lower()
    return any(host == name or host.endswith("." + name) for name in HOSTS)


def page_id(html: str) -> str | None:
    """Номер тайтла из мета-тега страницы."""
    match = _PAGE_ID_RE.search(html or "")
    return match.group(1) if match else None


def videos_url(base: str, number: str) -> str:
    """Адрес открытого списка видео.

    `base` — схема и хост страницы: у сайта есть зеркала (`old.`, `ru.`),
    и ходить надо на то же зеркало, что открыл человек.
    """
    parts = urllib.parse.urlsplit(base)
    return f"{parts.scheme}://{parts.netloc}/api/anime/{number}/videos"


FIRST_EPISODE = 1
"""Какую серию открываем по умолчанию.

API отдаёт плоский список: 13 серий × ~23 озвучки = 284 записи. Считать их
все «озвучками» нельзя — человек увидел бы список из 284 пунктов, где одна
и та же озвучка повторяется на каждой серии. Поэтому дорожки берём только
у первой серии, как и anidub.org: сериал открывается с начала, а озвучка —
выбор внутри серии.
"""


def kodik_embeds(data: Any) -> list[dict[str, str]]:
    """Озвучки Kodik ПЕРВОЙ серии. Остальные плееры нам не по силам.

    Возвращает список `{"iframe": …, "dubbing": …}` в порядке сайта: в
    браузере человек видит именно его, и менять порядок значит удивлять.

    Серия определяется полем `number`: у Kodik-записей оно заполнено
    (проверено: у «Блича» номера 1…13), а если сайт его не дал — берём всё,
    что есть, и не гадаем.
    """
    items = (data or {}).get("response") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    kodik_items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        info = item.get("data") or {}
        player = str(info.get("player") or "")
        iframe = str(item.get("iframe_url") or "")
        if "kodik" not in player.lower() and "kodik" not in iframe.lower():
            continue
        kodik_items.append((item, info, iframe))
    numbers = {str(item.get("number")) for item, _, _ in kodik_items}
    if str(FIRST_EPISODE) in numbers:
        first = str(FIRST_EPISODE)
        kodik_items = [row for row in kodik_items if str(row[0].get("number")) == first]
    # Одна и та же ссылка встречается под разными подписями (25 озвучек на
    # 20 адресов): у сайта часть дорожек продублирована. Показывать два
    # пункта, за которыми один поток, значит обещать выбор, которого нет.
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for _item, info, iframe in kodik_items:
        url = iframe if iframe.startswith("http") else "https:" + iframe
        if url in seen:
            continue
        seen.add(url)
        found.append({"iframe": url, "dubbing": str(info.get("dubbing") or "")})
    return found


class YummyAnimeSource:
    """Разбор страниц old.yummyani.me через их открытый API."""

    name = _NAME
    state = SourceState.PARTIAL
    note = "через Kodik; плееры Alloha и CVH не поддерживаются, региональные блоки не обходим"

    def __init__(self) -> None:
        self._inner = KodikSource()
        self._titles: dict[str, str] = {}
        """Название тайтла со страницы. Плеер отдаёт в `title` слово «Kodik»,
        и без этого в «Недавних» вместо аниме стояло имя плеера."""

        self._players: dict[str, set[str]] = {}
        """Плееры, найденные на странице — для текста отказа."""

        self._cache: dict[str, list[dict[str, str]]] = {}
        """Дорожки Kodik по адресу страницы. Список 218 КБ и переключение
        озвучки — повторный разбор: без кэша на каждый клик качали бы его
        заново."""

    # ---------------------------------------------------------------- поиск

    def matches(self, url: str) -> bool:
        if not is_yummy(url) or "/catalog/item/" not in url:
            return False
        return bool(self._tracks(url))

    def _tracks(self, page_url: str) -> list[dict[str, str]]:
        cached = self._cache.get(page_url)
        if cached is not None:
            return cached
        try:
            html = fetch_text(page_url)
            number = page_id(html)
            if not number:
                return []
            raw = fetch_text(videos_url(page_url, number))
            data = json.loads(raw)
        except (FetchError, json.JSONDecodeError):
            return []
        tracks = kodik_embeds(data)
        if tracks:
            self._cache[page_url] = tracks
        self._players[page_url] = players_present(data)
        self._titles[page_url] = page_title(html)
        return tracks

    def players_of(self, page_url: str) -> set[str]:
        """Какие плееры есть на странице: {"Kodik", "Alloha", "CVH"}.

        Нужно для честного текста: если Kodik на тайтле нет, а есть Alloha,
        человек должен узнать, что дело в конкретном плеере, а не в том,
        что мы «не умеем yummyani».
        """
        self._tracks(page_url)
        return self._players.get(page_url, set())

    # --------------------------------------------------------------- разбор

    def resolve(self, url: str, translation_index: int = 0) -> Stream:
        tracks = self._tracks(url)
        if not tracks:
            players = self.players_of(url)
            who = ", ".join(sorted(players)) or "неизвестный плеер"
            raise SourceFailedError(
                _NAME,
                f"у этого тайтла видео только через {who}, этот плеер не поддерживается",
                fix=(
                    "Мы умеем плеер Kodik — им сайт отдаёт большинство тайтлов. "
                    "Alloha закрывает часть видео по региону, CVH здесь устроен иначе, "
                    "чем на anidub.org. Это ограничение, а не сбой: обойти его нельзя"
                ),
                state=SourceState.PARTIAL,
            )
        index = translation_index if 0 <= translation_index < len(tracks) else 0
        stream = self._inner.resolve(tracks[index]["iframe"])
        return Stream(
            source=_NAME,
            title=self._titles.get(url) or stream.title,
            kind=stream.kind,
            qualities=stream.qualities,
            headers=stream.headers,
            duration_s=stream.duration_s,
            is_direct_file=stream.is_direct_file,
            audio_tracks=tuple(
                {"id": str(i), "title": track["dubbing"], "hash": "", "type": "seria"}
                for i, track in enumerate(tracks)
            ),
            subtitles=stream.subtitles,
        )


def region_blocked(url: str) -> bool:
    """Ссылка ведёт на плеер, который сам говорит «недоступно в регионе».

    Нужна не для разбора, а для честного текста: человек должен видеть
    причину, а не «источник не смог разобрать ссылку».
    """
    return "alloha." in urllib.parse.urlsplit(url).netloc.lower()
