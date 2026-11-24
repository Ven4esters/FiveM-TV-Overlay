"""tvigle.ru — российский кинотеатр, официальный API плеера.

Разбор короткий, потому что сайт сам всё рассказывает:

    GET https://www.tvigle.ru/video/<slug>/
        → <meta property="og:video" content="https://cloud.tvigle.ru/video/5838418/?…">

    GET https://cloud.tvigle.ru/api/play/video/5838418/?partner_id=24
        → {"playlist":{"items":[{"title":…, "durationMilliseconds":…,
                                 "videos":{"…":"https://…m3u8"}, "isGeoBlocked":false}]}}

Чего здесь НЕ надо: ни cookies, ни токена, ни подписи. `partner_id=24` —
открытая константа сайта (тот же параметр стоит в `og:video`).

**Главная особенность — регион.** У tvigle лицензия на РФ, и с зарубежного
адреса API отвечает `isGeoBlocked: true`, `videos: {}` и готовым текстом
«Ограничение по территории». Это не поломка и не ошибка разбора: так
работает лицензия. Из России API отдаёт ссылки. Поэтому источник честно
помечен `PARTIAL` и в отказе цитирует причину со стороны сайта — человек
должен видеть, что дело в лицензии, а не в нас.

Выбор озвучки у tvigle нет: одна дорожка на видео, потому что это
лицензионный показ, а не сборник дубляжей. Требование «есть выбор
озвучки» здесь выполнить нечем — и это тоже нужно сказать прямо.
"""

from __future__ import annotations

import json
import re
import urllib.parse

from ..core.errors import SourceFailedError
from ..core.models import Quality, SourceState, Stream, StreamKind
from ..web.fetch import FetchError, fetch_text
from .kodik_embed import page_title

HOSTS = ("tvigle.ru",)
"""Домены сайта (у него бывает www.)."""

API_BASE = "https://cloud.tvigle.ru/api/play/video/{video_id}/?partner_id=24"
"""Адрес плеера. `partner_id=24` — публичный параметр tvigle, не наш ключ."""

MIN_HEIGHT = 144
"""Ниже этого варианты в мастере — служебные дорожки (аудио, превью), а не
качество: показывать «144p» человеку бессмысленно."""

_NAME = "Tvigle"

_VIDEO_ID_RE = re.compile(r"cloud\.tvigle\.ru/video/(\d+)")
"""Номер видео из `og:video` — единственное надёжное место, где он есть."""

_M3U8_RE = re.compile(r"https?://[^\s\"']+\.m3u8[^\s\"']*")


def is_tvigle(url: str) -> bool:
    """Ссылка на страницу видео tvigle."""
    host = urllib.parse.urlsplit(url).netloc.lower()
    return any(host == name or host.endswith("." + name) for name in HOSTS)


def video_id(html: str) -> str | None:
    """Номер видео из мета-тега `og:video`."""
    match = _VIDEO_ID_RE.search(html or "")
    return match.group(1) if match else None


def stream_urls(videos: object) -> list[str]:
    """Ссылки на потоки из поля `videos`.

    Поле приходит словарём, где ключ — качество («hls», «240»…), а значение —
    адрес. Ключи не документированы, поэтому берём только те значения, что
    похожи на m3u8: остальное (постеры, реклама) нам не нужно.

    Порядок — от высокого качества к низкому: ключ-число и есть высота
    («720» → 720), а «hls»/«auto» — мастер без явной высоты, они идут
    первыми. Раньше порядок был «как в словаре», то есть по воле сайта:
    первым мог оказаться 240p, и мы разбирали качества именно из него.
    """
    found: list[tuple[int, str]] = []
    if not isinstance(videos, dict):
        return []
    for key, value in videos.items():
        text = str(value or "")
        if ".m3u8" not in text:
            continue
        digits = re.sub(r"\D", "", str(key))
        rank = int(digits) if digits else 10_000  # «hls»/«auto» — мастер, он выше всех
        if text not in [u for _, u in found]:
            found.append((rank, text))
    found.sort(key=lambda item: item[0], reverse=True)
    return [url for _, url in found]


def play_item(data: object) -> dict:
    """Первый элемент плейлиста — сам фильм. Пусто, если API отдал отказ."""
    playlist = (data or {}).get("playlist") if isinstance(data, dict) else None
    items = (playlist or {}).get("items") if isinstance(playlist, dict) else None
    if not isinstance(items, list) or not items:
        return {}
    first = items[0]
    return first if isinstance(first, dict) else {}


def geo_message(item: dict) -> str:
    """Текст отказа со стороны сайта — без HTML и лишних пробелов."""
    raw = str(item.get("errorMessage") or "")
    text = re.sub(r"<[^>]+>", " ", raw)
    return " ".join(text.split())


_STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF:(?P<attrs>[^\n]*)\n(?P<url>[^\s#][^\n]*)")
"""Вариант качества: строка атрибутов и адрес на следующей строке."""

_RESOLUTION_RE = re.compile(r"RESOLUTION=(?P<w>\d+)x(?P<h>\d+)")
"""Готовая высота кадра. Есть не всегда."""

_BANDWIDTH_RE = re.compile(r"(?:AVERAGE-)?BANDWIDTH=(?P<value>\d+)")
"""Битрейт. Есть почти всегда — по нему и оцениваем качество, если высоты нет."""

BANDWIDTH_TO_HEIGHT = (
    (6_000_000, 1080),
    (2_200_000, 720),
    (1_200_000, 480),
    (0, 360),
)
"""Битрейт → примерная высота кадра.

Зачем: у части релизов tvigle в мастере нет `RESOLUTION`, только
`BANDWIDTH`. Первая версия разбора требовала `RESOLUTION` и возвращала
НОЛЬ качеств — мост отвечал 502 «у потока нет качеств», и человек видел
«ошибка потока» на исправном видео. Оценка по битрейту грубая, но лучше
пустого списка: переключение качества всё равно делает сам плеер.
"""


def _height_from_bitrate(bits: int) -> int:
    """Оценить высоту кадра по битрейту."""
    for threshold, height in BANDWIDTH_TO_HEIGHT:
        if bits >= threshold:
            return height
    return 360


def qualities_from_master(master: str) -> list[Quality]:
    """Разобрать мастер-плейлист tvigle в качества.

    Высоту берём из `RESOLUTION`, если она есть; иначе оцениваем по
    битрейту. Пропускаем только то, что совсем не похоже на качество
    (ни высоты, ни битрейта).
    """
    qualities: list[Quality] = []
    for match in _STREAM_INF_RE.finditer(master or ""):
        attrs = match.group("attrs")
        url = match.group("url").strip()
        if not url or url.startswith("#"):
            continue
        size = _RESOLUTION_RE.search(attrs)
        band = _BANDWIDTH_RE.search(attrs)
        bits = int(band.group("value")) if band else 0
        if size:
            width = int(size.group("w"))
            height = int(size.group("h"))
        elif bits:
            height = _height_from_bitrate(bits)
            width = round(height * 16 / 9)
        else:
            continue
        if height < MIN_HEIGHT:
            continue
        bitrate = (
            max(1, round(bits / 1000)) if bits else max(1, round(width * height * 25 / 10_000))
        )
        qualities.append(Quality(height=height, width=width, bitrate_kbps=bitrate, url=url))
    qualities.sort(key=lambda q: q.height, reverse=True)
    return qualities


class TvigleSource:
    """Лицензионный кинотеатр: один поток, без выбора озвучки."""

    name = _NAME
    state = SourceState.PARTIAL
    note = "лицензия на РФ: из России играет, из-за границы сайт закрывает видео"

    def matches(self, url: str) -> bool:
        """Только по домену — страницу не качаем.

        Проверка дешёвая специально: `matches` вызывается для каждой ссылки
        по цепочке, и сетевая проверка здесь замедлила бы любой разбор.
        """
        return is_tvigle(url)

    def resolve(self, url: str, translation_index: int = 0) -> Stream:  # noqa: ARG002
        """Озвучки у tvigle нет: это лицензионный показ, а не сборник
        дубляжей. Параметр принимаем ради общего контракта источников."""
        try:
            html = fetch_text(url)
        except FetchError as exc:
            raise SourceFailedError(
                _NAME,
                "страница не открылась",
                fix="Проверьте ссылку в браузере — возможно, сайт просит войти",
                state=SourceState.PARTIAL,
            ) from exc

        number = video_id(html)
        if not number:
            raise SourceFailedError(
                _NAME,
                "на странице нет номера видео",
                fix="Нужна страница фильма вида tvigle.ru/video/<название>/",
                state=SourceState.PARTIAL,
            )

        try:
            raw = fetch_text(API_BASE.format(video_id=number))
            data = json.loads(raw)
        except (FetchError, json.JSONDecodeError) as exc:
            raise SourceFailedError(
                _NAME,
                "плеер не отдал данные",
                fix="Сайт мог сменить API — пришлите ссылку разработчику",
                state=SourceState.PARTIAL,
            ) from exc

        item = play_item(data)
        urls = stream_urls(item.get("videos"))
        if not urls:
            # Причина со стороны сайта: либо регион, либо снятая лицензия.
            why = geo_message(item) or "сайт не отдал ни одной ссылки на поток"
            raise SourceFailedError(_NAME, why, fix=_region_advice(item), state=SourceState.PARTIAL)

        title = str(item.get("title") or "") or page_title(html)
        duration_ms = int(item.get("durationMilliseconds") or 0)
        best = urls[0]
        qualities = qualities_from_master(safe_fetch(best)) if best.endswith(".m3u8") else []
        if not qualities:
            # Мастер не скачался или в нём ни одного варианта — это НЕ повод
            # для 502 (живой баг 24.11.2026: заказчик видел «master.m3u8:
            # HTTP 502» на исправном видео).
            #
            # Высоту НЕ выдумываем: Quality запрещает нули, и правильно —
            # ABR верит числам (ERR-067). Если `videos` — не мастер, а набор
            # по качествам, ключ и есть реальная высота («720» → 720).
            qualities = [_fallback_quality(best, item.get("videos"))]
        return Stream(
            source=_NAME,
            title=title,
            kind=StreamKind.HLS,
            qualities=tuple(qualities),
            duration_s=round(duration_ms / 1000),
        )


def _region_advice(item: dict) -> str:
    """Что сказать человеку, когда видео не отдано."""
    if item.get("isGeoBlocked"):
        return (
            "Это ограничение лицензии: tvigle показывает фильм только в России. "
            "Из-за границы его не открыть — обход региональных блокировок мы не делаем"
        )
    return "Сайт не отдал поток. Проверьте, открывается ли видео в браузере"


def safe_fetch(url: str) -> str:
    """Мастер-плейлист, который может и не отдаться: качество — не главное."""
    try:
        return fetch_text(url)
    except FetchError:
        return ""


def _fallback_quality(url: str, videos: object) -> Quality:
    """Одно качество из адреса, когда мастер не разобрался.

    Высота обязана быть настоящей (Quality не принимает нули, ERR-067).
    Источники правды по порядку:

      1. ключ в `videos`, совпадающий с этим адресом: у tvigle это высота
         («720») либо слово «hls»/«auto» для мастера;
      2. иначе 720x1280 — самый частый формат tvigle. Это подпись в списке
         качеств, а не заявление о битрейте: ABR смотрит на BANDWIDTH,
         который мост возьмёт из настоящего мастера.
    """
    height = 0
    if isinstance(videos, dict):
        for key, value in videos.items():
            if str(value or "") != url:
                continue
            digits = re.sub(r"\D", "", str(key))
            if digits:
                height = int(digits)
    if not height:
        height = 720  # только подпись: реальную высоту даст мастер в мосте
    width = round(height * 16 / 9)
    return Quality(
        height=height,
        width=width,
        # Та же оценка, что в `base.pick_hls_qualities`, когда сайт не назвал
        # битрейт: ~0.1 бит на пиксель кадра. Это догадка, и она записана
        # здесь честно — ABR всё равно уточнит по настоящему мастеру.
        bitrate_kbps=max(1, round(width * height * 30 / 10_000)),
        url=url,
        nominal_height=height,
    )
