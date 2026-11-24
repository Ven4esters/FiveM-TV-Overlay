"""Общие фикстуры для тестов.

Правило: unit-тесты не ходят в сеть и не зависят от времени (п. 7 инструкции).
Всё внешнее подменяется здесь же.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Тесты должны видеть пакет без установки: src в пути.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def qt_widgets():
    """QApplication — или пропуск всего модуля, если Qt не запускается.

    Лежит здесь, а не в отдельном модуле: каталог `tests` не является
    пакетом (у него нет __init__.py), поэтому `from tests.qt_support import …`
    падает с ModuleNotFoundError — проверено на CI. conftest.py pytest
    загружает сам, и он виден всем тестам.

    Зачем вообще проверка: `pytest.importorskip("PySide6")` смотрит только
    на наличие пакета. На headless-раннере пакет стоит, а системных
    библиотек (libEGL.so.1 и подобных) нет — падает импорт QtWidgets, и это
    ImportError рвёт сбор тестов или валит каждый тест модуля. В CI так
    упали 5 тестов и 2 модуля при полностью рабочем коде.
    """
    pytest.importorskip("PySide6", reason="PySide6 не установлен")
    try:
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    except ImportError as exc:  # libEGL.so.1 и родственники
        pytest.skip(
            f"Qt не запускается: нет системных GUI-библиотек ({exc})",
            allow_module_level=True,
        )
    return QApplication
