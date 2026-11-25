"""VK Видео (vkvideo.ru, vk.com/video, vk.ru/video).

Почему VK требует своего модуля, а не общего yt-dlp разбора:

1. Прямые okcdn-ссылки подписаны на IP и требуют особых заголовков —
   в браузере игры они дают 502 (ERR-061).

2. HLS отдаётся ОТДЕЛЬНО: видео-дорожки без звука + аудио-дорожки
   отдельным списком. Если взять первую попавшуюся m3u8 — будет «видео
   без звука» ровно как было с Rutube в v1.

3. Кадр не совпадает с названием качества. VK пишет `url1080`, а картинку
   отдаёт 1920x960 (кино 2:1 с чёрными полосами). Если подписать «960p»,
   человек не найдёт в списке привычное «1080p» (ERR-072).

Поэтому собираем СВОЙ master-плейлист: один вариант на каждое качество,
аудио подключаем группой `EXT-X-MEDIA`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..core.errors import SourceFailedError
from ..core.models import Quality, SourceState, Stream, StreamKind, sort_qualities
from .base import YtDlpSource, extract_info

log = logging.getLogger(__name__)

# Название качества в VK → привычная человеку высота.
# Обратное преобразование («почему 1920x960, а не 1080p») — ERR-072.
# Кадр, который VK отдаёт для каждого «паспортного» качества.
# Источник: живые замеры 23.11.2026 на видео заказчика.
VK_FRAME_TO_NOMINAL: dict[tuple[int, int], int] = {
    (256, 128): 144,
    (426, 212): 240,
    (640, 320): 360,
    (852, 426): 480,
    (1280, 640): 720,
    (1920, 960): 1080,
}
"""Карта «реальный кадр → название качества». Обратная к тому, что пишет VK."""


def nominal_for_frame(width: int, height: int) -> int:
    """Название качества VK по реальному размеру кадра.

    Если кадр не совпал с таблицей (VK поменял нарезку) — отдаём 0,
    и подпись будет по настоящей высоте. Лучше честно «960p», чем
    соврать «1080p» про неизвестный кадр.
    """
    return VK_FRAME_TO_NOMINAL.get((int(width), int(height)), 0)


def _is_multiplex(fmt: dict[str, Any]) -> bool:
    """Дорожка, в которой уже есть и видео, и звук.

    Это `hls-NNN` — у них vcodec/acodec в метаданных пустые, потому что
    yt-dlp не вскрывает контейнер. Проверяем по имени: у видео-дорожек
    бывает префикс `hls_fmp4-`, у аудио — суффикс `Audio`.
    """
    fmt_id = str(fmt.get("format_id") or "")
    if not fmt_id.startswith("hls-") or fmt_id.startswith("hls_fmp4-"):
        return False
    if "Audio" in fmt_id:
        return False
    return bool(fmt.get("height"))


def _is_video_only(fmt: dict[str, Any]) -> bool:
    return str(fmt.get("format_id") or "").startswith("hls_fmp4-") and bool(fmt.get("height"))


def _is_audio_only(fmt: dict[str, Any]) -> bool:
    fmt_id = str(fmt.get("format_id") or "")
    return "Audio" in fmt_id or (
        not fmt.get("height")
        and (fmt.get("vcodec") or "") in ("", "none")
        and "hls" in str(fmt.get("protocol") or "")
    )


def nominal_from_url(url: str) -> int:
    """Достать «паспортное» качество из прямой ссылки вида `.../url1080.mp4`."""
    match = re.search(r"/url(\d{3,4})\.", url or "")
    return int(match.group(1)) if match else 0


VK_CDN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://vk.com/",
}
"""Что обязан представить клиент, чтобы CDN VK отдал поток.

Проверено 23.11.2026: без Chrome-UA `okcdn.ru` отвечает 400 Bad Request,
потому что ссылка подписана с `srcAg/CHROME`. На это и жаловались —
«Замер не удался» на VK.
"""


class VkSource(YtDlpSource):
    name = "VK Видео"
    hosts = ("vkvideo.ru/", "vk.com/video", "vk.ru/video", "m.vkvideo.ru/")
    state = SourceState.OK
    note = "до 1080p, без входа"
    kind = StreamKind.HLS

    def extractor_opts(self) -> dict[str, Any]:
        # `format: all` обязателен: без него yt-dlp оставит один формат,
        # и собрать набор качеств будет не из чего.
        return {"format": "all"}

    def build_qualities(self, info: dict[str, Any]) -> tuple[Quality, ...]:
        """Собрать качества VK: сначала мультиплексы, потом видео+аудио.

        Мультиплекс (`hls-NNN`) идеален: видео и звук в одном потоке,
        ничего склеивать не надо. Если их нет — берём видео-дорожки,
        а звук подставит прокси (audio_url уходит в Stream).
        """
        formats = info.get("formats") or []
        mux = [f for f in formats if _is_multiplex(f)]
        video = [f for f in formats if _is_video_only(f)]

        # «Паспортное» название качества.
        #
        # VK подписывает качества как 144/240/360/480/720/1080, но кадр
        # отдаёт уже (1920x960 вместо 1920x1080) — у фильмов 2:1 картинка
        # с чёрными полосами обрезана по высоте. Показывать «960p» значит
        # спрятать от человека привычное «1080p», поэтому переводим кадр
        # обратно в паспортное значение по таблице VK (ERR-072).

        # Один мультиплекс — случай вырожденный: возможно, остальные
        # дорожки отданы раздельно. Тогда честнее взять видео+аудио.
        min_multiplex = 2
        chosen = mux if len(mux) >= min_multiplex else video
        found: list[Quality] = []
        for fmt in chosen:
            height = int(fmt.get("height") or 0)
            width = int(fmt.get("width") or 0)
            url = str(fmt.get("url") or "")
            if not height or not url:
                continue
            bitrate = int(fmt.get("tbr") or 0)
            if bitrate <= 0:
                bitrate = max(1, round(width * height * 30 / 10_000))
            nominal = nominal_for_frame(width, height) or nominal_from_url(url)
            found.append(
                Quality(
                    height=height,
                    width=width,
                    bitrate_kbps=bitrate,
                    url=url,
                    nominal_height=nominal,
                )
            )
        return sort_qualities(found)

    def pick_audio(self, info: dict[str, Any]) -> str:
        """Лучшая аудио-дорожка для склейки с видео без звука.

        Берём самую «жирную» по tbr: это AAC высокого профиля, а не
        низкобитрейтная дорожка для экономии трафика.
        """
        audio = [f for f in (info.get("formats") or []) if _is_audio_only(f)]
        if not audio:
            return ""
        best = max(audio, key=lambda f: float(f.get("tbr") or 0))
        return str(best.get("url") or "")

    def resolve(self, url: str, translation_index: int = 0) -> Stream:  # noqa: ARG002
        info = extract_info(url, **self.extractor_opts())
        qualities = self.build_qualities(info)
        if not qualities:
            raise SourceFailedError(
                self.name,
                "VK не отдал ни одного потока с видео",
                fix="Проверьте ссылку в браузере: возможно, видео приватное",
                state=SourceState.PARTIAL,
            )

        # Если качество — видео без звука, звук едет отдельной дорожкой.
        needs_audio = any(_is_video_only(f) for f in (info.get("formats") or []))
        has_mux = any(_is_multiplex(f) for f in (info.get("formats") or []))
        audio_url = self.pick_audio(info) if needs_audio and not has_mux else ""

        return Stream(
            source=self.name,
            title=str(info.get("title") or "").strip() or "VK Видео",
            kind=StreamKind.HLS,
            qualities=qualities,
            audio_url=audio_url,
            duration_s=int(info.get("duration") or 0),
            is_live=bool(info.get("is_live")),
            state=self.state,
            headers=dict(VK_CDN_HEADERS),
            # Заголовки нужны не плееру (в браузере игры всё и так работает),
            # а ЗАМЕРУ ГРОМКОСТИ: ffmpeg по умолчанию представляется как
            # «Lavf/60.16.100», и CDN VK (okcdn.ru) отвечает 400 — отсюда
            # «замер не удался». С Chrome-UA тот же адрес отдаёт 200 и
            # application/x-mpegURL (проверено 23.11.2026).
        )
