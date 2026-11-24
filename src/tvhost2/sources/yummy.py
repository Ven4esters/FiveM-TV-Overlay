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

На одном тайтле плееры смешаны, и каждый разбирается так:

  - **Kodik** — ссылка на плеер уже в JSON, отдаём внутреннему Kodik
    (у «Блича» 13 серий × 27 озвучек);
  - **CVH** (cdnvideohub) — здесь ссылка ведёт на страницу-обёртку
    `ru.yummyani.me/iframeCVH.html?dubbing_code=…&anime_id=…&episode=N`.
    В HTML той страницы плеера нет: его собирает скрипт, и параметры
    (`pub`, `aggr`, `title_id`) зашиты в бандле. Прочитано из бандла
    24.11.2026: `data-aggregator="mali"`, `data-publisher-id=745`,
    `data-title-id=<anime_id>`, `priority-voice=<dubbing_code>`,
    `episode=<episode>`. Дальше — тот же API, что у anidub.org, поэтому
    разбор один общий (`cvh.py`), а не вторая копия;
  - **другие плееры** (у сайта их несколько) — их у нас нет, и это
    говорится прямо, а не замалчивается.

Поэтому озвучки обоих плееров складываются в ОДИН список: человеку не
важно, из какого плеера студия, ему важен выбор студии. Если плеера нет
вовсе — честный отказ с названием плеера, а не молчаливая пустота.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from ..core.errors import SourceFailedError
from ..core.models import SourceState, Stream
from ..web.fetch import FetchError, fetch_text
from . import cvh
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
    фразы «доступно только через <плеер>».
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
    matches_costly = True
    """`matches` тянет JSON тайтла (218 КБ), чтобы понять, есть ли там Kodik.
    В общей очереди такой ответ недопустим: он удорожает любую чужую ссылку.
    Реестр спрашивает этот источник последним."""
    note = "через Kodik; остальные плееры сайта не поддерживаются, региональные блоки не обходим"

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
        """Дорожки ОБОИХ плееров одним списком: `{"iframe","dubbing","player"}`.

        Человеку не важно, из какого плеера студия — ему важен выбор
        студии. Поэтому Kodik и CVH стоят подряд, в порядке сайта: сначала
        всё, что дал Kodik, затем CVH. Внутри каждого — порядок сайта.

        `player` нужен разбору: Kodik получает ссылку готового плеера,
        а CVH — ссылку обёртки, из которой параметры ещё надо достать.
        """
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
        tracks = [{**track, "player": "kodik"} for track in kodik_embeds(data)]
        tracks += [{**track, "player": "cvh"} for track in cvh_embeds(data)]
        if tracks:
            self._cache[page_url] = tracks
        self._players[page_url] = players_present(data)
        self._titles[page_url] = page_title(html)
        return tracks

    def url_with_episode(self, url: str, number: int) -> str:
        """Номер серии — в адресе страницы сайта: своей серии у yummy нет."""
        from .kodik import url_with_episode  # noqa: PLC0415

        return url_with_episode(url, number)

    def players_of(self, page_url: str) -> set[str]:
        """Какие плееры есть на странице: {"Kodik", "CVH", …}.

        Нужно для честного текста: если Kodik на тайтле нет, а есть другой
        плеер, человек должен узнать, что дело в конкретном плеере, а не в
        том, что мы «не умеем yummyani». Имена не фиксированы: сайт может
        добавить новый плеер, и он попадёт в текст отказа сам.
        """
        self._tracks(page_url)
        return self._players.get(page_url, set())

    # --------------------------------------------------------------- разбор

    def resolve(self, url: str, translation_index: int = 0, episode: int | None = None) -> Stream:
        tracks = self._tracks(url)
        if not tracks:
            players = self.players_of(url)
            who = ", ".join(sorted(players)) or "неизвестный плеер"
            raise SourceFailedError(
                _NAME,
                f"у этого тайтла видео только через {who}, этот плеер не поддерживается",
                fix=(
                    "Мы умеем плееры Kodik и CVH — ими сайт отдаёт большинство "
                    "тайтлов. Остальные устроены иначе, и часть закрыта по "
                    "региону. Это ограничение, а не сбой: обойти его нельзя"
                ),
                state=SourceState.PARTIAL,
            )
        index = translation_index if 0 <= translation_index < len(tracks) else 0
        chosen = tracks[index]
        stream = self._play(chosen, episode)
        return Stream(
            source=_NAME,
            title=self._titles.get(url) or stream.title,
            kind=stream.kind,
            qualities=stream.qualities,
            headers=stream.headers,
            duration_s=stream.duration_s,
            is_direct_file=stream.is_direct_file,
            # Озвучки — ОБЩИЙ список обоих плееров: индекс пришёл из него
            # же, и человек не должен видеть другой список после выбора.
            audio_tracks=tuple(
                {"id": str(i), "title": track["dubbing"], "hash": "", "type": "seria"}
                for i, track in enumerate(tracks)
            ),
            subtitles=stream.subtitles,
            # Серии — от выбранного плеера как есть: сайт отдаёт сериал,
            # а список серий знает плеер.
            episodes=stream.episodes,
            episode=stream.episode,
            episode_season=stream.episode_season,
            can_switch_episode=stream.can_switch_episode,
        )

    def _play(self, track: dict[str, str], episode: int | None) -> Stream:
        """Поток выбранной дорожки: Kodik или CVH — по имени плеера.

        Разница только в том, откуда берутся параметры: у Kodik ссылка на
        плеер уже готова, у CVH ссылка ведёт на страницу-обёртку, и
        параметры (pub/aggr/title_id) надо достать из её адреса. Дальше
        разбор общий, поэтому источник сам ничего про CVH не знает.
        """
        if track.get("player") != "cvh":
            return self._inner.resolve(track["iframe"], episode=episode)
        params = cvh_params(track["iframe"])
        if not params:
            raise SourceFailedError(
                _NAME,
                "в ссылке CVH нет номера тайтла",
                fix="Скопируйте ссылку на тайтл заново — сайт мог поменять адрес",
                state=SourceState.PARTIAL,
            )
        number = episode
        if number is None and params["episode"].isdigit():
            number = int(params["episode"])
        return cvh.build_stream(
            source_name=_NAME,
            site="old.yummyani.me",
            params=cvh.CvhParams(pub=CVH_PUB, aggr=CVH_AGGR, title_id=params["anime_id"]),
            base_title=self._titles.get(track["iframe"]) or "",
            translation_index=0,
            episode=number,
            cache_key=f"yummy:{params['anime_id']}",
            # Подпись okcdn привязана к UA: ставим тот же Firefox, что и в API.
            stream_headers={"User-Agent": cvh.FIREFOX_UA, "Referer": "https://old.yummyani.me/"},
        )


# ----------------------------------------------------------------------
# CVH: второй плеер сайта
# ----------------------------------------------------------------------

CVH_PUB = "745"
"""`data-publisher-id` из бандла iframeCVH. Прочитано 24.11.2026:
`{"data-aggregator":"mali","data-title-id":...,"data-publisher-id":745}`.
Это номер сайта yummyanime в API cdnvideohub, у anidub.org свой (`19`)."""

CVH_AGGR = "mali"
"""`data-aggregator` yummyanime. У anidub.org `cvh` — и это единственное
отличие двух сайтов друг от друга внутри одного и того же API."""


def is_cvh_iframe(url: str) -> bool:
    """Ссылка ведёт на страницу-обёртку CVH, а не на готовый плеер."""
    return "iframecvh" in (url or "").lower()


def cvh_params(iframe_url: str) -> dict[str, str]:
    """Параметры CVH из адреса обёртки: `anime_id`, `episode`, `dubbing_code`.

    Пустой словарь — в адресе нет `anime_id`: без него API ответит мусором,
    и лучше сказать «не нашёл плеер», чем разбирать мусор.

    Внимание на имя: `anime_id` у сайта — это НЕ `page_id` каталога
    (61967 против 21747 у одного и того же тайтла). Брать `page_id` вместо
    него нельзя: API вернёт чужой тайтл.
    """
    parsed = urllib.parse.urlsplit(iframe_url or "")
    query = urllib.parse.parse_qs(parsed.query)
    anime_id = (query.get("anime_id") or [""])[0].strip()
    if not anime_id:
        return {}
    return {
        "anime_id": anime_id,
        "episode": (query.get("episode") or [""])[0].strip(),
        "dubbing_code": (query.get("dubbing_code") or [""])[0].strip(),
    }


def cvh_embeds(data: Any) -> list[dict[str, str]]:
    """Озвучки CVH ПЕРВОЙ серии: `{"iframe", "dubbing"}` в порядке сайта.

    Почему только первая серия: API отдаёт плоский список «серия × озвучка»,
    и на 13 серий с 20 студиями это 260 пунктов в пульте. Озвучка — выбор
    ВНУТРИ серии, поэтому список студий берём у первой.
    """
    items = (data or {}).get("response") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        info = item.get("data") or {}
        player = str(info.get("player") or "")
        iframe = str(item.get("iframe_url") or "")
        if "cvh" not in player.lower() and not is_cvh_iframe(iframe):
            continue
        rows.append(item)
    numbers = {str(item.get("number")) for item in rows}
    if str(FIRST_EPISODE) in numbers:
        rows = [item for item in rows if str(item.get("number")) == str(FIRST_EPISODE)]
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in rows:
        url = str(item.get("iframe_url") or "")
        url = url if url.startswith("http") else "https:" + url
        key = str((item.get("data") or {}).get("dubbing") or url)
        if key in seen:
            continue
        seen.add(key)
        found.append({"iframe": url, "dubbing": str((item.get("data") or {}).get("dubbing") or "")})
    return found
