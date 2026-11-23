"""Источники. Один модуль — один сайт.

Регистрация здесь, в одном месте. Порядок важен: специфичные сайты
проверяются раньше общих (`vkvideo.ru` не должен уходить в «прямой HLS»).
"""

from __future__ import annotations

from ..core import registry
from .base import YtDlpSource, extract_info, pick_direct_formats, pick_hls_qualities
from .rutube import RutubeSource, video_id

__all__ = [
    "RutubeSource",
    "YtDlpSource",
    "extract_info",
    "pick_direct_formats",
    "pick_hls_qualities",
    "register_all",
    "video_id",
]


def register_all() -> None:
    """Зарегистрировать все готовые источники. Вызывается один раз при старте."""
    if "Rutube" in registry.names():
        return  # уже зарегистрированы — повторный вызов безопасен
    registry.register(RutubeSource())
