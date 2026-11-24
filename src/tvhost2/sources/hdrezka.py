"""hdrezka — обёртка над собственным подписанным CDN (этап 10 плана).

Как устроен сайт (проверено 24.11.2026):

1. **Вход защищён Anubis** — JS-задачей «Проверяем, что вы не бот!».
   Обычный GET отдаёт заглушку, и это нормальное состояние, а не поломка:
   браузер решает задачу сам за ~8 секунд. Поэтому страницу открывает не
   fetch, а браузерная сессия. Тот же приём, что у Kodik.

2. **Поток отдаёт AJAX** `POST /ajax/get_cdn_series/` с полями
   `id`, `translator_id`, `action=get_movie`, `is_tv`, `cdn_quality`.
   Ответ — JSON, где `url` это master-плейлист с подписью, а `quality`
   перечисляет доступные качества. Плеер встраивается в `#player`, адрес
   потока в HTML не лежит.

3. **Главная засада — гео.** С зарубежного IP (проверено из США) сервер
   отвечает `premium_content: 1` и `url: false` **и для премиум-фильмов,
   и для обычных сериалов**, а в плеере пишет «К сожалению, это видео не
   доступно в вашем регионе». Отличить «нельзя из этой страны» от «сайт
   сломался» критично: это разные ответы человеку. Поэтому строку плеера
   читаем отдельно и переводим в GeoblockedError.

   В России (страна заказчика) тот же запрос отдаёт нормальный поток —
   именно поэтому источник имеет смысл и проверяется через
   `check_link.bat` на машине заказчика, а не с сервера разработки.

4. **Проверку бота не обходим.** Anubis решается сам, обычным браузером;
   ничего разгадывать и подделывать не нужно — это просто ожидание.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..core.errors import GeoblockedError, SourceFailedError
from ..core.models import Quality, SourceState, Stream, StreamKind, sort_qualities

log = logging.getLogger(__name__)

_AJAX = "/ajax/get_cdn_series/"
"""Один AJAX на фильм и сериал; разница только в `is_tv`."""

_HOST_INDEX = 2
"""В `https://host/path` имя домена — третья часть после split('/')."""

_REGION_RE = re.compile(r"недоступно в вашем регионе|не доступно в вашем регионе", re.IGNORECASE)
"""Строка, которой сайт объясняет отказ. Проверена на живой странице."""

_INIT_RE = re.compile(r"initCDN(?:Movies|Series)Events\(\s*(\d+)\s*,\s*(\d+)")
_TRANSLATOR_RE = re.compile(
    r'<li[^>]*data-translator_id="(\d+)"[^>]*>([^<]{0,80})</li>', re.IGNORECASE
)

BOT_CHECK_MARKERS = ("Проверяем, что вы не бот", "Доступ запрещён", "Just a moment")
"""Признаки страницы-заглушки. Показываются, пока не отработает JS-задача."""


def is_hdrezka(url: str) -> bool:
    """Ссылка ведёт на hdrezka?

    Зеркал много (`hdrezka.ag`, `hdrezka-home.tv`, `rezka.ag`, …), и они
    меняются. Узнаём по имени домена, а не по списку: иначе каждое новое
    зеркало — это правка кода.
    """
    low = (url or "").lower()
    if not low.startswith(("http://", "https://")):
        return False
    parts = low.split("/")
    host = parts[2] if len(parts) > _HOST_INDEX else ""
    # «rezka.» — и hdrezka, и rezka.ag; «hdrezka» ловит зеркала вида
    # hdrezka-home.tv, где слово стоит первым. Чужие домены не задеваем:
    # пример «rezka.by» — это резка металла, а не кино, но он и не
    # зарегистрирован в интернете как источник, ссылок оттуда не бывает.
    return "rezka." in host or host.startswith(("hdrezka", "www.hdrezka"))


def video_id_from(html: str) -> str:
    """id фильма из вызова плеера на странице."""
    match = _INIT_RE.search(html or "")
    return match.group(1) if match else ""


def translator_id_from(html: str) -> str:
    """id озвучки по умолчанию (первый активный перевод)."""
    active = re.search(
        r'<li[^>]*b-prem_translator active[^>]*data-translator_id="(\d+)"', html or ""
    )
    if active:
        return active.group(1)
    match = _TRANSLATOR_RE.search(html or "")
    return match.group(1) if match else ""


def parse_qualities(payload: dict[str, Any]) -> tuple[Quality, ...]:
    """JSON от /ajax/get_cdn_series/ → качества.

    `quality` в ответе — текст вида `<li data-value="1080">1080p</li>…`,
    то есть список доступных высот. Ни ширины, ни битрейта сайт не даёт:
    ширину считаем по пропорции 16:9, битрейт — нижнюю границу для H.264,
    чтобы ABR не застрял на низком уровне (ERR-067).
    """
    master = str(payload.get("url") or "")
    if not master:
        return ()
    quality_list = str(payload.get("quality") or "")
    heights = sorted({int(h) for h in re.findall(r'data-value="(\d+)"', quality_list)})
    if not heights:
        heights = [720]
    items: list[Quality] = []
    for height in heights:
        width = round(height * 16 / 9)
        items.append(
            Quality(
                height=height,
                width=width,
                bitrate_kbps=max(1, round(width * height * 30 / 10_000)),
                url=master,
            )
        )
    return sort_qualities(items)


def title_from(html: str) -> str:
    """Название из <title> без хвоста «смотреть онлайн…»."""
    match = re.search(r"<title>([^<]*)</title>", html or "", re.IGNORECASE)
    if not match:
        return ""
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    for marker in (" смотреть онлайн", " онлайн бесплатно"):
        cut = title.lower().find(marker)
        if cut > 0:
            title = title[:cut].strip()
    return title


def explain_payload(payload: dict[str, Any], html: str) -> str:
    """Почему потока нет: регион, премиум или неожиданный ответ.

    Возвращает "" если всё в порядке (есть url). Иначе — готовую причину
    для человека. Разделение важно: «недоступно в регионе» и «сайт
    сломался» требуют разных действий, а по одному `url: false` их
    не различить.
    """
    if payload.get("url"):
        return ""
    if _REGION_RE.search(html or ""):
        return "region"
    if payload.get("premium_content"):
        return "premium"
    message = str(payload.get("message") or "").strip()
    return message or "сайт вернул ответ без адреса потока"


class HdrezkaSource:
    """hdrezka: страница → id → AJAX → master-плейлист."""

    name = "hdrezka"
    state = SourceState.PARTIAL
    """Не OK: без браузерной сессии страница не открывается, а зарубежный IP
    видит «недоступно в регионе». Честное состояние — работает с оговоркой."""
    note = "фильмы и сериалы, из России, без входа"
    kind = StreamKind.HLS

    def matches(self, url: str) -> bool:
        return is_hdrezka(url)

    def resolve(self, _url: str) -> Stream:
        """Ссылка на страницу → поток.

        Браузерную сессию здесь не поднимаем: она живёт в web/browser.py
        и стоит дорого. Источник рассчитывает, что вызывающий код уже
        получил HTML страницы через неё, и передаёт его в `resolve_html`.
        Так источник остаётся чистым и тестируемым без сети.
        """
        raise SourceFailedError(
            self.name,
            "hdrezka открывается только браузерной сессией",
            fix="Откройте ссылку через кнопку «Через браузер» в интерфейсе",
            state=SourceState.BROWSER_ONLY,
        )

    def resolve_html(self, html: str, fetch_json: Any) -> Stream:
        """Собрать поток из уже полученного HTML страницы.

        fetch_json — функция (path, data) → dict, которую даёт браузерная
        сессия: AJAX обязан уйти с её cookies и Referer, иначе Anubis
        отвечает заглушкой вместо JSON.
        """
        video_id = video_id_from(html)
        if not video_id:
            if any(marker in html for marker in BOT_CHECK_MARKERS):
                raise SourceFailedError(
                    self.name,
                    "страница ещё не открылась: сайт проверяет браузер",
                    fix="Подождите несколько секунд и попробуйте снова",
                    state=SourceState.BROWSER_ONLY,
                )
            raise SourceFailedError(
                self.name,
                "на странице нет плеера",
                fix="Похоже, это не страница фильма: нужна ссылка вида hdrezka.ag/films/…",
                state=SourceState.PARTIAL,
            )

        translator = translator_id_from(html) or "0"
        is_tv = "1" if "/series/" in html or "initCDNSeriesEvents" in html else "0"
        payload = fetch_json(
            _AJAX,
            {
                "id": video_id,
                "translator_id": translator,
                "is_camrip": "0",
                "is_ads": "0",
                "is_director": "0",
                "action": "get_movie",
                "favs": video_id,
                "is_tv": is_tv,
                "cdn_quality": "1080p",
            },
        )
        reason = explain_payload(payload, html)
        if reason == "region":
            raise GeoblockedError(self.name)
        if reason:
            raise SourceFailedError(
                self.name,
                reason,
                fix="Выберите другое видео или проверьте ссылку в браузере",
                state=SourceState.PARTIAL,
            )

        qualities = parse_qualities(payload)
        if not qualities:
            raise SourceFailedError(
                self.name,
                "в ответе нет ни одного качества",
                fix="Пришлите ссылку разработчику",
                state=SourceState.BROKEN,
            )
        return Stream(
            source=self.name,
            title=title_from(html) or "hdrezka",
            kind=self.kind,
            qualities=qualities,
            state=self.state,
        )


def payload_text(payload: Any) -> str:
    """Ответ AJAX одной строкой — для журнала."""
    return json.dumps(payload, ensure_ascii=False)[:300]
