"""Импорт моделей через web — чтобы у модулей не было двух путей.

`web` работает с теми же `Stream`/`Quality`, что и источники. Реэкспорт
здесь нужен, чтобы импорты внутри web читались как локальные, но
единственным владельцем структур оставался core.
"""

from ..core.models import Quality, SourceState, Stream, StreamKind

__all__ = ["Quality", "SourceState", "Stream", "StreamKind"]
