"""Ядро: модели, ошибки, реестр. Никаких сетевых зависимостей."""

from .errors import (
    GeoblockedError,
    LinkNotRecognisedError,
    LiveEndedError,
    NeedsCookiesError,
    NoQualitiesError,
    OverlayError,
    SourceFailedError,
)
from .models import Quality, SourceState, Stream, StreamKind, sort_qualities
from .registry import Registry, Source, registry

__all__ = [
    "GeoblockedError",
    "LinkNotRecognisedError",
    "LiveEndedError",
    "NeedsCookiesError",
    "NoQualitiesError",
    "OverlayError",
    "Quality",
    "Registry",
    "Source",
    "SourceFailedError",
    "SourceState",
    "Stream",
    "StreamKind",
    "registry",
    "sort_qualities",
]
