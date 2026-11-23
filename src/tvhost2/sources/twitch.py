"""Twitch — самый простой источник, и это приятная новость после YouTube.

Что отдаёт Twitch (замеры 23.11.2026):

* **живой канал** — HLS, мультиплекс: `160p / 360p / 480p / 720p60 /
  1080p60__source_`, в каждой дорожке сразу h264 + AAC;
* **запись (VOD)** — то же самое, плюс бывает и `720p`, и `720p60` на одной
  высоте (берём ту, что жирнее по битрейту);
* **клип** — прямые mp4-файлы без HLS, качество 360/480/720.

Склеивать ничего не нужно, аудиогруппы не нужно — просто мастер из дорожек.
Отдельный случай — канал не в эфире: это не поломка, а нормальное состояние,
и человек должен узнать, что записи при этом доступны.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..core.errors import LiveEndedError, SourceFailedError
from ..core.models import Quality, SourceState, Stream, StreamKind, sort_qualities
from .base import YtDlpSource, extract_info, pick_hls_qualities

log = logging.getLogger(__name__)

_CHANNEL_RE = re.compile(r"twitch\.tv/([A-Za-z0-9_]{3,25})/?$")
_VOD_RE = re.compile(r"twitch\.tv/videos/(\d+)")
_CLIP_RE = re.compile(r"(?:twitch\.tv/\w+/clip/|clips\.twitch\.tv/)([\w-]+)")


# Высоты, на которых Twitch отдаёт клипы. Всё остальное — служебное.
CLIP_HEIGHTS = (360, 480, 720, 1080)


def pick_clip_formats(formats: list[dict[str, Any]]) -> tuple[Quality, ...]:
    """Качества клипа: прямые mp4 без кодеков, ориентир — высота."""
    found: list[Quality] = []
    for fmt in formats:
        height = int(fmt.get("height") or 0)
        url = str(fmt.get("url") or "")
        if height not in CLIP_HEIGHTS or not url:
            continue
        width = int(fmt.get("width") or 0) or round(height * 16 / 9)
        bitrate = int(fmt.get("tbr") or 0)
        if bitrate <= 0:
            # Клипы короткие; оценка по площади кадра, чтобы ABR не падал
            # на 360p из-за «нулевого» битрейта.
            bitrate = max(1, round(width * height * 30 / 10_000))
        found.append(Quality(height=height, width=width, bitrate_kbps=bitrate, url=url))
    return sort_qualities(found)


def link_kind(url: str) -> str:
    """Что за ссылка: канал, запись, клип — до похода в сеть."""
    low = (url or "").lower()
    if _VOD_RE.search(low):
        return "vod"
    if _CLIP_RE.search(low):
        return "clip"
    if "player.twitch.tv" in low:
        return "player"
    if _CHANNEL_RE.search(low):
        return "channel"
    return "unknown"


def channel_name(url: str) -> str:
    """Имя канала из ссылки (для понятных сообщений)."""
    match = _CHANNEL_RE.search((url or "").split("?")[0])
    return match.group(1) if match else ""


class TwitchSource(YtDlpSource):
    name = "Twitch"
    hosts = ("twitch.tv/", "clips.twitch.tv/", "ttvnw.net/", "player.twitch.tv/")
    state = SourceState.OK
    note = "стримы, записи, клипы"
    kind = StreamKind.HLS

    def extractor_opts(self) -> dict[str, Any]:
        return {"format": "all"}

    def build_qualities(self, info: dict[str, Any]) -> tuple[Quality, ...]:
        """Сначала HLS (стримы и записи), потом прямые файлы (клипы).

        `pick_hls_qualities` сам отбросит превью-раскадровки (mhtml) и
        аудио-дорожку без видео — в Twitch они тоже приходят в общем списке.
        """
        qualities = pick_hls_qualities(info.get("formats") or [])
        if qualities:
            return qualities
        # Клипы Twitch приходят прямыми mp4, но yt-dlp НЕ заполняет у них
        # vcodec/acodec. Ориентир — height: превью-раскадровки (sb0/sb1)
        # идут через mhtml и высоты в нужном диапазоне не имеют.
        return pick_clip_formats(info.get("formats") or [])

    def resolve(self, url: str) -> Stream:
        info = extract_info(url, **self.extractor_opts())
        qualities = self.build_qualities(info)

        if not qualities:
            raise SourceFailedError(
                self.name,
                "Twitch не отдал ни одного качества",
                fix="Проверьте ссылку в браузере — возможно, запись удалена",
                state=SourceState.PARTIAL,
            )

        return Stream(
            source=self.name,
            title=str(info.get("title") or "").strip() or "Twitch",
            kind=StreamKind.HLS,
            qualities=qualities,
            duration_s=int(info.get("duration") or 0),
            is_live=bool(info.get("is_live")),
            state=self.state,
        )

    def matches(self, url: str) -> bool:
        """Ссылка на Twitch, но не на служебные страницы."""
        if not super().matches(url):
            return False
        low = (url or "").lower()
        # Каталоги и настройки — не видео.
        return not any(part in low for part in ("/directory", "/settings", "/subscriptions"))


def offline_error(url: str) -> LiveEndedError:
    """Канал не в эфире — ошибка с подсказкой про записи."""
    who = channel_name(url)
    error = LiveEndedError("Twitch")
    error.why = f"канал {who} сейчас не в эфире" if who else "канал сейчас не в эфире"
    error.fix = "Записи этого канала обычно доступны: вставьте ссылку вида twitch.tv/videos/<номер>"
    return error
