"""YouTube — самый сложный источник, и вот почему.

Что отдаёт YouTube (замеры 23.11.2026):

* видео — HLS-плейлисты (itag 269/229/230/229…270), **без звука**;
* аудио — HLS-плейлист (itag 233/234), но сегменты внутри — **сырые AAC
  файлы** (`ID3` в начале, не MPEG-TS). hls.js такой поток в группе
  `EXT-X-MEDIA` не проиграет;
* мультиплекса (видео+звук сразу) нет ни в VOD, ни в live.

Значит, собрать «правильный» master-плейлист нельзя: браузер получит
картинку без звука. Поэтому единственный надёжный путь — склеить дорожки
на своей стороне через ffmpeg в режиме `-c copy` (без перекодирования).

Проверено: 10 минут 144p склеиваются за ~5 секунд, на выходе h264 + AAC.
Цена — ffmpeg должен быть на компьютере (на Windows кладём рядом с .exe,
в pip-пакете imageio-ffmpeg он уже есть).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..core.errors import SourceFailedError
from ..core.models import Quality, SourceState, Stream, StreamKind, sort_qualities
from .base import YtDlpSource, extract_info

log = logging.getLogger(__name__)

# itag → человеческое понятие. Берём только H.264 (avc1): браузер игры
# (CEF в GTA 5) не умеет VP9/AV1 в аппаратном режиме и часто вообще не умеет.
H264_ITAGS: dict[str, tuple[int, int, int]] = {
    "269": (256, 144, 290),
    "229": (426, 240, 546),
    "230": (640, 360, 1210),
    "231": (854, 480, 1569),
    "232": (1280, 720, 2969),
    "270": (1920, 1080, 5421),
}
"""itag → (ширина, высота, ориентировочный битрейт кбит/с)."""

# Аудио-дорожки YouTube в HLS. 233/234 — единственные, что отдаются
# плейлистом; остальные (140, 251) — прямые файлы, их в мастер не вставить.
AUDIO_HLS_ITAGS = ("233", "234")

_LIVE_MARKERS = ("/live", "live_stream", "is_live")


class YouTubeSource(YtDlpSource):
    name = "YouTube"
    hosts = (
        "youtube.com/watch",
        "youtube.com/live",
        "youtube.com/shorts",
        "youtu.be/",
        "m.youtube.com/watch",
        "youtube-nocookie.com",
    )
    state = SourceState.OK
    note = "до 1080p, живые стримы"
    kind = StreamKind.HLS_SPLIT

    def extractor_opts(self) -> dict[str, Any]:
        return {"format": "all"}

    # ------------------------------------------------------------------
    # выбор дорожек
    # ------------------------------------------------------------------

    @staticmethod
    def _video_hls(info: dict[str, Any]) -> dict[int, dict[str, Any]]:
        """HLS-дорожки с видео, по одной на высоту. Только H.264."""
        best: dict[int, dict[str, Any]] = {}
        for fmt in info.get("formats") or []:
            fmt_id = str(fmt.get("format_id") or "")
            if "m3u8" not in str(fmt.get("protocol") or ""):
                continue
            height = int(fmt.get("height") or 0)
            url = str(fmt.get("url") or "")
            if not height or not url:
                continue
            vcodec = str(fmt.get("vcodec") or "")
            if not vcodec.startswith("avc1"):
                continue  # VP9/AV1 браузер игры не тянет
            prev = best.get(height)
            if prev is None or float(fmt.get("tbr") or 0) > float(prev.get("tbr") or 0):
                best[height] = {**fmt, "format_id": fmt_id}
        return best

    @staticmethod
    def _audio_hls(info: dict[str, Any]) -> dict[str, Any] | None:
        """Аудио-плейлист (itag 233/234). yt-dlp зовёт их `vcodec=none`."""
        for fmt in info.get("formats") or []:
            if str(fmt.get("format_id") or "") in AUDIO_HLS_ITAGS and "m3u8" in str(
                fmt.get("protocol") or ""
            ):
                return fmt
        # Запасной: любая HLS-дорожка без высоты и без видеокодека.
        for fmt in info.get("formats") or []:
            if (
                "m3u8" in str(fmt.get("protocol") or "")
                and not fmt.get("height")
                and (fmt.get("vcodec") or "none") == "none"
            ):
                return fmt
        return None

    def build_qualities(self, info: dict[str, Any]) -> tuple[Quality, ...]:
        found: list[Quality] = []
        for height, fmt in self._video_hls(info).items():
            width = int(fmt.get("width") or 0) or round(height * 16 / 9)
            bitrate = int(fmt.get("tbr") or 0)
            if bitrate <= 0:
                # Ориентир из таблицы itag — он точнее, чем «площадь кадра».
                itag = str(fmt.get("format_id") or "")
                bitrate = H264_ITAGS.get(itag, (0, 0, 0))[2] or round(width * height * 30 / 10_000)
            found.append(
                Quality(
                    height=height,
                    width=width,
                    bitrate_kbps=bitrate,
                    url=str(fmt["url"]),
                )
            )
        return sort_qualities(found)

    def resolve(self, url: str) -> Stream:
        info = extract_info(url, **self.extractor_opts())
        if info.get("is_live"):
            raise SourceFailedError(
                self.name,
                "прямой эфир YouTube пока не поддерживается",
                fix=(
                    "Живые трансляции требуют склейки на лету — это следующий этап. "
                    "Обычные видео и записи работают"
                ),
                state=SourceState.PARTIAL,
            )

        qualities = self.build_qualities(info)
        audio = self._audio_hls(info)
        if not qualities:
            raise SourceFailedError(
                self.name,
                "YouTube не отдал ни одного H.264-потока",
                fix="Пришлите ссылку разработчику — возможно, у видео только VP9/AV1",
                state=SourceState.PARTIAL,
            )
        if audio is None:
            raise SourceFailedError(
                self.name,
                "YouTube не отдал аудио-дорожку",
                fix="Попробуйте другую ссылку: у некоторых видео звук отдаётся только в браузере",
                state=SourceState.PARTIAL,
            )

        return Stream(
            source=self.name,
            title=str(info.get("title") or "").strip() or "YouTube",
            kind=StreamKind.HLS_SPLIT,
            qualities=qualities,
            audio_url=str(audio.get("url") or ""),
            duration_s=int(info.get("duration") or 0),
            is_live=False,
            state=self.state,
        )


def is_live_link(url: str) -> bool:
    """Похоже ли на прямую трансляцию — до сети, по виду ссылки."""
    low = (url or "").lower()
    return any(marker in low for marker in _LIVE_MARKERS)


def video_id(url: str) -> str:
    """11-символьный id видео из любого вида ссылки YouTube."""
    match = re.search(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})", url or "")
    return match.group(1) if match else ""
