"""
Иконки интерфейса.

`_icon` раньше был методом только у `Overlay`, но использовался и в
`NoteWindow` — окно-подсказка падало бы с AttributeError при открытии.
Поэтому функция вынесена на уровень модуля: одна реализация для всех окон.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon, QPixmap

# Иконки лежат в папке icons/ рядом с src/ (см. иерархию в CONTRIBUTING).
# ui/ → tvhost/ → src/ → корень проекта
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ICONS_DIR = PROJECT_ROOT / "icons"


def icon(name: str, _size: int = 20) -> QIcon:
    """
    Иконка из папки icons/ по имени без расширения.

    Возвращает пустой QIcon, если файла нет: интерфейс не должен падать
    из-за отсутствующей картинки (её могли не скопировать при установке).
    """
    path = ICONS_DIR / f"{name}.svg"
    if not path.is_file():
        return QIcon()
    return QIcon(str(path))


def pixmap(name: str, size: int = 16) -> QPixmap:
    """Иконка, готовая к вставке в QLabel."""
    return icon(name, size).pixmap(size, size)
