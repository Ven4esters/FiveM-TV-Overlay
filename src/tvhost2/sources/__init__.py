"""Источники. Один модуль — один сайт.

Регистрация здесь, в одном месте. Порядок важен: специфичные сайты
проверяются раньше общих (`vkvideo.ru` не должен уходить в «прямой HLS»).
"""

from __future__ import annotations

from ..core import registry
from .base import YtDlpSource, extract_info, pick_direct_formats, pick_hls_qualities
from .kodik import KodikRef, KodikSource, decrypt_link, parse_player_url, rot18
from .rutube import RutubeSource, video_id
from .twitch import TwitchSource, channel_name, link_kind
from .vk import VkSource, nominal_from_url
from .youtube import YouTubeSource

__all__ = [
    "KodikRef",
    "KodikSource",
    "RutubeSource",
    "TwitchSource",
    "VkSource",
    "YouTubeSource",
    "YtDlpSource",
    "channel_name",
    "decrypt_link",
    "extract_info",
    "link_kind",
    "nominal_from_url",
    "parse_player_url",
    "pick_direct_formats",
    "pick_hls_qualities",
    "register_all",
    "rot18",
    "video_id",
]


def register_all() -> None:
    """Зарегистрировать все готовые источники. Вызывается один раз при старте."""
    if "Rutube" in registry.names():
        return  # уже зарегистрированы — повторный вызов безопасен
    registry.register(RutubeSource())
    registry.register(VkSource())
    registry.register(YouTubeSource())
    registry.register(KodikSource())
    registry.register(TwitchSource())
