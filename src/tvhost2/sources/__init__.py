"""Источники. Один модуль — один сайт.

Регистрация здесь, в одном месте. Порядок важен: специфичные сайты
проверяются раньше общих (`vkvideo.ru` не должен уходить в «прямой HLS»).
"""

from __future__ import annotations

from ..core import registry
from .anidub import AnidubSource, is_anidub
from .anidub_org import AnidubOrgSource, is_anidub_org
from .base import YtDlpSource, extract_info, pick_direct_formats, pick_hls_qualities
from .kodik import KodikRef, KodikSource, decrypt_link, parse_player_url, rot18
from .kodik_embed import KodikEmbedSource
from .okru import OkRuSource, is_okru
from .rutube import RutubeSource, video_id
from .tvigle import TvigleSource, is_tvigle
from .twitch import TwitchSource, channel_name, link_kind
from .vk import VkSource, nominal_from_url
from .youtube import YouTubeSource
from .yummy import YummyAnimeSource, is_yummy

__all__ = [
    "AnidubOrgSource",
    "AnidubSource",
    "KodikEmbedSource",
    "KodikRef",
    "KodikSource",
    "OkRuSource",
    "RutubeSource",
    "TvigleSource",
    "TwitchSource",
    "VkSource",
    "YouTubeSource",
    "YtDlpSource",
    "YummyAnimeSource",
    "channel_name",
    "decrypt_link",
    "extract_info",
    "is_anidub",
    "is_anidub_org",
    "is_okru",
    "is_tvigle",
    "is_yummy",
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
    # Встройка — ПОСЛЕ прямого Kodik: ссылка на сам плеер должна достаться
    # точному источнику с озвучками, а обёртка берётся только за страницы
    # сайтов. Порядок здесь и есть приоритет.
    registry.register(KodikEmbedSource())
    registry.register(YummyAnimeSource())
    registry.register(TvigleSource())
    registry.register(TwitchSource())
    registry.register(OkRuSource())
    registry.register(AnidubSource())
    registry.register(AnidubOrgSource())
