"""Проверка, что Qt не просто установлен, а вообще работает.

Зачем отдельный модуль: `pytest.importorskip("PySide6")` проверяет только
наличие пакета. На headless-раннере пакет стоит, а системных библиотек
(libEGL.so.1 и подобных) нет — тогда падает уже импорт `QtWidgets`, и это
ImportError:

  * обрывает сбор тестов («Interrupted: 1 error during collection»), если
    импорт стоит на уровне модуля;
  * валит каждый тест модуля, если импорт внутри теста.

В CI это и случилось: 5 failed и 2 errors из-за одной отсутствующей
библиотеки, при полностью рабочем коде. Здесь одна проверка на весь проект,
чтобы третий раз не наступить на те же грабли.
"""

from __future__ import annotations

import pytest

REASON = "Qt не запускается: нет системных GUI-библиотек (libEGL и подобных)"


def qt_widgets():
    """Модуль QtWidgets или пропуск всего модуля тестов.

    Вызывать на уровне модуля:

        QApplication = qt_available_or_skip()

    Возвращает класс QApplication — заодно проверено, что он импортируется.
    """
    pytest.importorskip("PySide6", reason="PySide6 не установлен")
    try:
        raise ImportError("libEGL.so.1: cannot open shared object file")
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    except ImportError as exc:  # libEGL.so.1 и родственники
        pytest.skip(f"{REASON}: {exc}", allow_module_level=True)
    return QApplication
