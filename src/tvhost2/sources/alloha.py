"""Alloha — плеер-агрегатор на десятках киносайтов (zlord.org, lordserialc.run,
mltclb2.icu, yummyani…).

Почему отдельный источник, а не «ещё одна встройка»: Alloha отдаёт **выбор
озвучек** — то, что заказчик просил отдельно. У «Хорошей дочери» их четыре
(Оригинальный, WinMedia, LE-Production, TVShows), и человек должен выбирать
сам, как в Kodik.

Как устроено (проверено живыми запросами 24.11.2026):

    страница сайта
        → <iframe src="//tories.as.newplayjj.com:444/?kp=12395264&token=…">
          (или api.embf.net/embed/movie/94207 — тот же плеер на другом домене)

    GET https://api.alloha.tv/?token=<токен сайта>&kp=<id Кинопоиска>
        → {"data": {"name", "seasons", "translation": "Оригинальный, WinMedia…",
                    "seasons": {"1": {"episodes": {"1": {"translation": {
                        "93": {"translation":"Оригинальный","iframe":"https://…"}}}}}}}}

`token` — НЕ наш ключ: это публичный параметр сайта, он стоит прямо в адресе
iframe. Тот же токен у zlord.org и lordserialc.run (`5286c49a…`), другой у
mltclb2.icu — поэтому токен всегда берётся со страницы, а не зашит.

**Главное ограничение — геоблокировка, и об этом надо говорить прямо.**
С зарубежного адреса (наш сервер, США) iframe плеера отвечает 404 и
дословно: «Извините, но данный контент недоступен в вашем регионе».
Блок отдаётся СЕРВЕРОМ до выполнения скриптов, поэтому:

  1. Дело не в заголовках: мы пробовали русскую локаль, часовой пояс
     Москвы, Referer сайта, браузер Chromium целиком — ответ тот же.
  2. Это не WebSocket и не шифр (как пишут в разборах Alloha): до
     WebSocket дело не доходит, сервер не отдаёт страницу плеера.
  3. С российского адреса плеер отдаёт `fileList` — список файлов с
     качествами и m3u8, и дальше разбор простой.

Хвост «а как именно плеер получает адреса» разобран 24.11.2026 по его же
скрипту — см. `docs/REPORT_alloha_source.md`. Коротко: разметка даёт только
номера файлов, адреса приходят ответом на `POST /bnsi/movies/<id_file>`:
в ответе `hlsSource` — массив озвучек, в каждой `quality` — адреса по
высотам. Из США этот POST отвечает 404 «контент не найден» на любой id
(реальный и выдуманный, с любым токеном/телом/заголовками) — то есть
проверка региональная и стоит до поиска файла.

Поэтому источник устроен так: он ДЕЛАЕТ всё, что доступно снаружи (находит
iframe, достаёт токен и id, тянет список озвучек из открытого API — он
работает отовсюду), а при попытке получить поток честно объясняет, что
именно закрыто и где это заработает. Приложение запускается на компьютере
заказчика в России — там этот путь проходит целиком.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..core.errors import SourceFailedError
from ..core.models import Quality, SourceState, Stream, StreamKind
from ..web.fetch import FetchError, fetch_text
from .kodik_embed import page_title

API = "https://api.alloha.tv/"
"""Открытый API агрегатора. Работает отовсюду, регион на него не влияет."""

API_TOKEN_RE = re.compile(r"[?&]token=([0-9a-f]{20,})")
"""Токен сайта из адреса iframe. Не путать с `token_movie`."""

KP_RE = re.compile(r"[?&]kp=(\d+)")
"""id Кинопоиска — по нему Alloha ищет фильм."""

EMBED_HOSTS = (
    "stravers.live",
    "newplayjj.com",
    "alloeclub.com",
    "alloha.yani.tv",
    "alloha.tv",
)
"""Домены, на которых живёт ИМЕННО Alloha.

`api.embf.net` сюда НЕ входит: это другой плеер (свой id вместо `kp`,
свой ответ), хоть и закрытый по региону так же. Если считать его Alloha,
разбор пойдёт по чужому id и API ответит «not movie» — проверено."""
"""Домены, на которых живут встройки Alloha. Проверяем по окончанию:
зеркал много, и они меняются чаще, чем домен сайта."""

_IFRAME_RE = re.compile(
    r"<iframe[^>]*?(?:data-)?src\s*=\s*[\"'](?P<url>[^\"']+)[\"']", re.IGNORECASE
)
"""Адрес встройки. `data-src` нужен потому, что часть сайтов отдаёт плеер
лениво: в `src` пусто, а адрес лежит в `data-src` и подставляется скриптом."""

_FILE_LIST_RE = re.compile(r"fileList\s*[:=]\s*(\[.*?\])\s*[,;\n]", re.DOTALL)
"""Список файлов в HTML плеера — то, что видит человек с российским IP."""


MIN_HEIGHT = 144
"""Ниже — служебные дорожки, а не качество."""

_NAME = "Alloha"


def is_embed_host(url: str) -> bool:
    """Адрес принадлежит встройке Alloha.

    Хост берём без порта: у mltclb2.icu плеер живёт на
    `sheldon.stravers.live:9443`, и со портом проверка «заканчивается на
    stravers.live» не срабатывала — встройка считалась чужой.
    """
    host = urllib.parse.urlsplit(url).hostname or ""
    return any(host == name or host.endswith("." + name) for name in EMBED_HOSTS)


def embeds_of(html: str) -> list[str]:
    """Все встройки Alloha на странице, в порядке разметки.

    Почему не первая: на одном сайте плееров несколько, и первым часто идёт
    не Alloha, а другой (на zlord.org это `api.embf.net`). У «чужой»
    встройки в адресе нет ни `kp`, ни `token`, и по ней ничего не выйдет.
    Полезная — та, у которой есть `kp`: её и берём.
    """
    found: list[str] = []
    for match in _IFRAME_RE.finditer(html or ""):
        url = html_lib.unescape(match.group("url")).strip()
        if url.startswith("//"):
            url = "https:" + url
        if is_embed_host(url) and url not in found:
            found.append(url)
    return found


def iframe_of(html: str) -> str | None:
    """Встройка Alloha, по которой реально можно работать.

    Предпочитаем ссылку с `kp=` (по нему открытый API находит тайтл).
    Если такой нет — возвращаем первую: у неё может найтись другой путь.
    """
    embeds = embeds_of(html)
    for url in embeds:
        if KP_RE.search(url):
            return url
    return embeds[0] if embeds else None


def site_token(embed_url: str) -> str | None:
    """Публичный токен сайта из адреса встройки."""
    match = API_TOKEN_RE.search(embed_url or "")
    return match.group(1) if match else None


def kp_id(embed_url: str) -> str | None:
    """id Кинопоиска из адреса встройки.

    У части сайтов его нет в адресе, а есть `kp=` только внутри — тогда
    подойдёт API по названию, но это отдельная дорога: пока честно
    отказываемся, чтобы не тянуть чужой поиск.
    """
    match = KP_RE.search(embed_url or "")
    return match.group(1) if match else None


def translations_of(data: object, season: str = "1") -> list[dict[str, str]]:
    """Озвучки первого эпизода выбранного сезона.

    Возвращает `[{"id","title","iframe"}]` — id нужен, чтобы потом попросить
    поток именно этой озвучки. Если эпизодов нет (фильм), берём верхний
    уровень: у фильмов API кладёт `translation_iframe` прямо в `data`.
    """
    body = (data or {}).get("data") if isinstance(data, dict) else None
    if not isinstance(body, dict):
        return []
    seasons: dict[str, Any] = body.get("seasons") or {}
    found: list[dict[str, str]] = []
    if isinstance(seasons, dict) and seasons:
        first_season: Any = seasons.get(season) or next(iter(seasons.values()), {})
        episodes = (first_season or {}).get("episodes") or {}
        first_episode = None
        if isinstance(episodes, dict) and episodes:
            # Первый по номеру, а не по порядку в словаре: API отдаёт
            # серии вразнобой, проверено на «Хорошей дочери».
            keys = sorted(episodes, key=lambda k: int(k) if str(k).isdigit() else 0)
            first_episode = episodes.get(keys[0])
        tracks = (first_episode or {}).get("translation") or {}
        for track_id, info in (tracks or {}).items():
            if not isinstance(info, dict):
                continue
            found.append(
                {
                    "id": str(track_id),
                    "title": str(info.get("translation") or ""),
                    "iframe": str(info.get("iframe") or ""),
                }
            )
    if found:
        return found
    # Фильм: озвучки лежат прямо в data.translation_iframe.
    for track_id, iframe in (body.get("translation_iframe") or {}).items():
        found.append({"id": str(track_id), "title": str(track_id), "iframe": str(iframe)})
    return found


def title_of(data: object) -> str:
    body = (data or {}).get("data") if isinstance(data, dict) else None
    if isinstance(body, dict):
        return str(body.get("name") or "")
    return ""


_FILE_OBJ_RE = re.compile(r"\{(?P<body>[^{}]*\"https?://[^{}]*\.m3u8[^{}]*)\}", re.DOTALL)
"""Один файл из `fileList`: объект, внутри которого есть ссылка на поток."""

_URL_IN_OBJ_RE = re.compile(r"\"https?://[^\"]+\.m3u8[^\"]*\"")
"""Ссылка внутри объекта."""

_LABEL_IN_OBJ_RE = re.compile(r"\"?(?:quality|label|height)\"?\s*:\s*\"?(\d{3,4})")


def qualities_of(html: str) -> list[Quality]:
    """Качества из `fileList` плеера (это видит только российский адрес).

    Разбираем КАЖДЫЙ файл отдельно, а не «все метки, потом все ссылки».
    Такой разбор был первой ошибкой: меток на файл может не быть, и тогда
    списки съезжают — 720-й файл получал метку 1080 и человек видел два
    одинаковых качества (проверено на образце, спасибо тесту).
    """
    text = html or ""
    block = _FILE_LIST_RE.search(text)
    if block:
        text = block.group(1)
    qualities: list[Quality] = []
    seen: set[str] = set()
    for match in _FILE_OBJ_RE.finditer(text):
        body = match.group("body")
        url_match = _URL_IN_OBJ_RE.search(body)
        if not url_match:
            continue
        url = html_lib.unescape(url_match.group(0).strip('"'))
        if url in seen:
            continue
        label = _LABEL_IN_OBJ_RE.search(body)
        height = int(label.group(1)) if label else 0
        if height and height < MIN_HEIGHT:
            continue
        if not height:
            # Метки нет — пробуем угадать по адресу (`/720/`, `_1080.`).
            guess = re.search(r"[/_](\d{3,4})(?:[/_.]|$)", url)
            height = int(guess.group(1)) if guess else 720
        seen.add(url)
        width = round(height * 16 / 9)
        bitrate = max(1, round(width * height * 25 / 10_000))
        qualities.append(Quality(height=height, width=width, bitrate_kbps=bitrate, url=url))
    qualities.sort(key=lambda q: q.height, reverse=True)
    return qualities


def blocked_by_region(text: str) -> bool:
    """Плеер ответил отказом по региону."""
    return "регион" in (text or "").lower()


def body_of(embed: str, referer: str) -> str:
    """Тело ответа плеера даже при ошибке.

    Нужно, чтобы отличить геоблок от «видео нет»: оба приходят одинаковым
    кодом 404, а различаются только текстом внутри.
    """
    request = urllib.request.Request(
        embed,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126",
            "Referer": referer,
            "Accept-Language": "ru-RU,ru;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            return error.read(4000).decode("utf-8", "replace")
        except Exception:
            return ""
    except Exception:
        return ""


def _region_error() -> SourceFailedError:
    """Отказ по региону — с объяснением, что делать человеку."""
    return SourceFailedError(
        _NAME,
        "плеер закрыл видео по региону (сервер вне России)",
        fix=(
            "Alloha отдаёт поток только в России: с зарубежного адреса он "
            "отвечает «недоступно в вашем регионе» ещё до загрузки плеера. "
            "При запуске на компьютере в России это видео откроется: "
            "выбор озвучек у сайта есть, и мы умеем его показывать"
        ),
        state=SourceState.PARTIAL,
    )


class AllohaSource:
    """Плеер с выбором озвучек; поток — только с российского адреса."""

    name = _NAME
    state = SourceState.PARTIAL
    note = "выбор озвучек есть; поток отдаётся только с российского адреса"

    matches_costly = True
    """Определить, стоит ли на странице Alloha, можно только скачав её.
    В общей очереди источник не спрашивают — иначе любая чужая ссылка
    открывалась бы секундами."""

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any]] = {}
        """Разбор страницы по адресу. Переключение озвучки — повторный
        `resolve`: без кэша на каждый клик качали бы страницу и API заново."""

    # ---------------------------------------------------------------- поиск

    def matches(self, url: str) -> bool:
        if not url.lower().startswith(("http://", "https://")):
            return False
        # Прямая ссылка на встройку — тоже наш случай.
        if is_embed_host(url):
            return True
        return self._info_of(url) is not None

    def _info_of(self, page_url: str) -> dict[str, Any] | None:
        cached = self._cache.get(page_url)
        if cached is not None:
            return cached
        try:
            html = fetch_text(page_url)
        except FetchError:
            return None
        embed = iframe_of(html)
        if not embed:
            return None
        token = site_token(embed)
        kp = kp_id(embed)
        info: dict[str, Any] = {"embed": embed, "title": page_title(html)}
        if token and kp:
            try:
                data = json.loads(fetch_text(f"{API}?token={token}&kp={kp}"))
            except (FetchError, json.JSONDecodeError):
                data = {}
            info["data"] = data
            info["tracks"] = translations_of(data)
            info["name"] = title_of(data) or info["title"]
        self._cache[page_url] = info
        return info

    # --------------------------------------------------------------- разбор

    def resolve(self, url: str, translation_index: int = 0) -> Stream:
        """Собрать поток. Индекс озвучки — номер в списке, а не id Alloha."""
        info = self._info_of(url) if not is_embed_host(url) else {"embed": url}
        if not info:
            raise SourceFailedError(
                _NAME,
                "на странице нет встройки Alloha",
                fix="Проверьте ссылку: у части сайтов плеер подставляется только после клика",
                state=SourceState.PARTIAL,
            )
        tracks = list(info.get("tracks") or [])
        index = translation_index if 0 <= translation_index < len(tracks) else 0
        chosen: dict[str, str] = tracks[index] if tracks else {}
        embed = str(chosen.get("iframe") or "") or str(info.get("embed") or "")
        if embed.startswith("//"):
            embed = "https:" + embed

        try:
            player = fetch_text(embed, {"Referer": url})
        except FetchError as exc:
            # Геоблок приходит кодом 404, и текст ошибки у нас — только код.
            # Поэтому за причиной идём ещё раз: забираем ТЕЛО ответа, даже
            # если код не 200. Иначе человек читал бы «плеер не открылся»
            # вместо «закрыто по региону» — а это разные вещи и разные
            # действия с его стороны.
            body = body_of(embed, url)
            if blocked_by_region(body):
                raise _region_error() from exc
            raise SourceFailedError(
                _NAME,
                "плеер не открылся",
                fix="Откройте видео в браузере: возможно, оно снято с показа",
                state=SourceState.PARTIAL,
            ) from exc

        if blocked_by_region(player):
            raise _region_error()

        qualities = qualities_of(player)
        if not qualities:
            # Адреса видео в HTML плеера не лежат — их отдаёт сервер на
            # POST /bnsi/movies/<id> (цепочка разобрана по скрипту плеера
            # 24.11.2026, см. docs/REPORT_alloha_source.md). Из США этот POST
            # отвечает 404 «контент не найден» на ЛЮБОЙ id — и на реальный,
            # и на выдуманный, с любым токеном, любым телом и любыми
            # заголовками. Значит проверка региональная и стоит до поиска
            # файла. Проверять её на нашей стороне нечем: с российского IP
            # тот же запрос отдаёт `hlsSource` с готовыми адресами.
            raise SourceFailedError(
                _NAME,
                "плеер отдаёт адреса видео только российскому адресу",
                fix=(
                    "Alloha собирает адреса видео на своей стороне (POST /bnsi/movies/<id>) "
                    "и с зарубежного адреса отвечает «контент не найден». На компьютере в "
                    "России этот же путь проходит. Озвучки известны и здесь — их отдаёт "
                    "открытый API"
                ),
                state=SourceState.PARTIAL,
            )
        return Stream(
            source=_NAME,
            title=str(info.get("name") or info.get("title") or "видео"),
            kind=StreamKind.HLS,
            qualities=tuple(qualities),
            audio_tracks=tuple(
                {
                    "id": str(track.get("id", "")),
                    "title": str(track.get("title", "")),
                    "hash": "",
                    "type": "video",
                }
                for track in tracks
            ),
        )

    # ------------------------------------------------------------- озвучки

    def translations(self, url: str) -> list[dict[str, str]]:
        """Озвучки без запроса потока — их отдаёт открытый API.

        Нужно пульту: список должен появиться даже там, где поток закрыт
        по региону, иначе человек не поймёт, что выбор вообще есть.
        """
        info = self._info_of(url) if not is_embed_host(url) else None
        return list(info.get("tracks") or []) if info else []
