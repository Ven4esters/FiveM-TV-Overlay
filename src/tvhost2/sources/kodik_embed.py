"""Сайты, которые показывают видео через плеер Kodik.

Зачем отдельный модуль. Kodik — это чужой плеер, который встраивают десятки
аниме- и киносайтов. У нас уже есть `kodik.py`, который умеет разобрать
ССЫЛКУ НА ПЛЕЕР (`kodikplayer.com/seria/…`). Но человек вставляет ссылку на
СТРАНИЦУ САЙТА (`ani-media.online/anime-seriali/1575-86.html`), а страница
сайта для нас — просто незнакомый домен, и мы честно отвечали «сайт не
поддерживается: ani-media.online».

Этот модуль закрывает разрыв: скачивает страницу, находит в ней встройку
Kodik и отдаёт разбор знакомому `kodik.py`. Правило простое: **сайт, у
которого плеер Kodik, поддерживается автоматически** — список доменов не
нужен, потому что плеер узнаётся по ссылке внутри страницы, а не по адресу.

Проверено живьём 24.11.2026 на `ani-media.online`:

    <iframe data-src="//kodikplayer.com/serial/37803/<hash>/720p"

Важная деталь, которая стоила часа: у СЕРИАЛОВ хэш в адресе iframe
подменяется, а настоящие `type/hash/id` лежат в переменных `vInfo` на
странице плеера. `kodik.py` теперь читает их; здесь мы просто передаём
ссылку целиком.

Чего модуль НЕ делает:

1. **Не обходит DDoS-Guard.** `animego.me` отвечает 500 на запросы с
   зарубежных адресов на любую страницу — мы это честно показываем
   человеку, а не притворяемся, что сайт сломан у него.
2. **Не ищет другие плееры.** Если на странице нет Kodik, модуль
   отказывается — и тогда вступает общий ответ «сайт не поддерживается».
3. **Не ходит по каталогу.** Нужна прямая ссылка на страницу с видео.
"""

from __future__ import annotations

import html as html_lib
import re
import urllib.parse
from dataclasses import replace
from typing import Any

from ..core.errors import SourceFailedError
from ..core.models import SourceState, Stream
from ..web.fetch import FetchError, fetch_text
from . import kodik
from .kodik import KodikSource

EMBED_HOSTS = ("kodikplayer.com", "kodik.info", "kodik.biz", "kodik.cc", "kodik.bz")
"""Домены плеера, которые считаем одной встройкой."""

_IFRAME_RE = re.compile(
    r"""(?:data-)?src\s*=\s*["'](?P<url>[^"']*kodik[a-z0-9\-]*\.[a-z]{2,}/[^"']+)["']""",
    re.IGNORECASE,
)
"""Ссылка на Kodik в атрибуте `src` или `data-src`.

`data-src` нужен потому, что ani-media отдаёт iframe лениво: в `src` пусто,
а адрес лежит в `data-src`, и браузер подставляет его скриптом. Без этого
совпадения страница выглядела бы «без плеера».
"""

_PLAYER_NAME = "Kodik (встройка)"

_TITLE_RE = re.compile(r"<title[^>]*>(?P<text>[^<]{1,300})</title>", re.IGNORECASE)
"""Заголовок страницы сайта. Плеер Kodik отдаёт в `title` просто «Kodik» —
человеку в пульте нужно название аниме, а не имя плеера."""

_SUFFIX_RE = re.compile(
    r"\s*[—–-]\s*(?:смотреть|аниме|онлайн|бесплатно).*$", re.IGNORECASE | re.DOTALL
)
"""Хвост вида «— смотреть аниме онлайн, все серии»: в заголовке списка он
съедает место, а смысла не несёт."""


def page_title(html: str) -> str:
    """Название с страницы: без рекламного хвоста и HTML-сущностей."""
    match = _TITLE_RE.search(html or "")
    if not match:
        return ""
    text = html_lib.unescape(match.group("text")).strip()
    text = _SUFFIX_RE.sub("", text).strip()
    return text[:120]


def find_embed(html: str) -> str | None:
    """Первая ссылка на плеер Kodik внутри страницы сайта."""
    match = _IFRAME_RE.search(html or "")
    if not match:
        return None
    url = match.group("url").strip()
    if url.startswith("//"):
        return "https:" + url
    return url


def is_embed_page(url: str) -> bool:
    """Отличать страницу сайта от ссылки на САМ плеер.

    Прямую ссылку разбирает `kodik.py` — там известны и имя, и озвучки.
    Здесь только страницы-обёртки, иначе источники спорят за один адрес и
    озвучки теряются: обёртка их не раздаёт.

    Проверять именно `parse_player_url` недостаточно: адрес `kodikplayer.com/
    seria/<id>/<hash>` без хвостового качества регулярка не ловит, и мы
    пошли бы качать страницу плеера как «сайт». Поэтому домен проверяется
    отдельно.
    """
    if not url.lower().startswith(("http://", "https://")):
        return False
    if kodik.parse_player_url(url) is not None:
        return False
    return host_of(url) not in EMBED_HOSTS


class KodikEmbedSource:
    """Страница любого сайта, внутри которой стоит плеер Kodik."""

    name = _PLAYER_NAME
    state = SourceState.OK
    matches_costly = True
    """Ответ требует скачать страницу: по адресу не угадать, есть ли внутри
    чужой плеер. Поэтому в общей очереди источник не спрашивают — иначе
    любой незнакомый сайт открывался бы секундами. Реестр зовёт его только
    после того, как быстрые проверки по строке отказали."""
    note = "сайты с плеером Kodik; DDoS-Guard и региональные блоки не обходим"

    def __init__(self) -> None:
        self._inner = KodikSource()
        self._titles: dict[str, str] = {}
        """Название страницы по адресу: плеер Kodik называет поток «Kodik»."""

        self._cache: dict[str, str] = {}
        """Адрес встройки по адресу страницы.

        Зачем: переключение озвучки — это повторный `resolve`. Без кэша мы
        заново качали бы страницу сайта на каждую дорожку (сотни КБ на клик),
        хотя внутри страницы адрес плеера не меняется в течение сессии.
        """

    # ---------------------------------------------------------------- поиск

    def matches(self, url: str) -> bool:
        """Соглашаемся только на то, где точно есть встройка.

        Проверка сетевая, а `matches` вызывается по цепочке для каждой
        ссылки, поэтому результат запоминается: страница скачивается один
        раз, а не на каждый вопрос «а ты умеешь?».
        """
        if not is_embed_page(url):
            return False
        return self._embed_of(url) is not None

    def _embed_of(self, page_url: str) -> str | None:
        cached = self._cache.get(page_url)
        if cached:
            return cached
        try:
            html = fetch_text(page_url)
        except FetchError:
            return None
        embed = find_embed(html)
        if embed:
            self._cache[page_url] = embed
            self._titles[page_url] = page_title(html)
        return embed

    # --------------------------------------------------------------- разбор

    def resolve(self, url: str, translation_index: int = 0, episode: int | None = None) -> Stream:
        """Страница сайта → поток. Серия и озвучка — насквозь во внутренний Kodik.

        Встройка ничего своего не добавляет: и озвучки, и серии уже умеет
        `KodikSource`. Здесь важно только не потерять их по дороге — иначе
        на всех сайтах с встройкой (а их большинство) переключатель серий
        молча пропал бы.
        """
        embed = self._embed_of(url)
        if not embed:
            raise SourceFailedError(
                _PLAYER_NAME,
                "на странице не нашлась встройка Kodik",
                fix="Откройте страницу в браузере: возможно, плеер отдаётся только после входа",
                state=SourceState.PARTIAL,
            )
        stream = self._inner.resolve(embed, translation_index=translation_index, episode=episode)
        # Имя источника подменяем на понятное человеку: в пульте он видит
        # «Kodik (встройка)», а не имя плеера, о котором не слышал. Название
        # берём со страницы: сам Kodik возвращает в `title` просто «Kodik»,
        # и в «Недавних» вместо аниме было бы имя плеера.
        title = self._titles.get(url) or stream.title
        return replace(stream, source=_PLAYER_NAME, title=title)

    def url_with_episode(self, url: str, number: int) -> str:
        """Номер серии в адрес САЙТА, а не плеера.

        У встройки ссылка на плеер синтезируется по странице, поэтому
        помнить номер надо в адресе страницы — inner-метод сюда не годится.
        """
        from .kodik import url_with_episode  # noqa: PLC0415

        return url_with_episode(url, number)

    # ---------------------------------------------------------- озвучки

    def translations(self, url: str) -> list[dict[str, Any]]:
        """Озвучки — это озвучки Kodik, отдаём как есть.

        Отдельного метода у KodikSource нет: список дорожек приходит вместе
        с потоком в `audio_tracks`. Поэтому пробуем разобрать и берём его.
        """
        embed = self._embed_of(url)
        if not embed:
            return []
        try:
            stream = self._inner.resolve(embed)
        except Exception:
            return []
        return [dict(track) for track in stream.audio_tracks]


def host_of(url: str) -> str:
    """Домен страницы — для сообщений человеку."""
    return urllib.parse.urlsplit(url).netloc.lower()
