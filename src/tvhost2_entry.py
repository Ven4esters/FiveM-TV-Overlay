"""Точка входа собранного .exe. Запускается PyInstaller из tvhost.spec.

Почему отдельный файл, а не `tvhost2/ui/__main__.py` напрямую: PyInstaller
исполняет указанный скрипт как модуль верхнего уровня, вне пакета. Внутри
`ui/__main__.py` относительные импорты (`from . import theme`,
`from ..core import resources`) в таком режиме падают с ImportError:
attempted relative import with no known parent package.

Здесь импорт абсолютный — `tvhost2.ui.__main__` грузится как часть пакета,
и все относительные импорты внутри него работают. Поведение то же, что у
`python -m tvhost2.ui`, то есть в .exe запускается ровно то же окно.

Из исходников этот файл тоже работает:
    python src/tvhost2_entry.py
"""

from __future__ import annotations

import sys
import traceback
from types import TracebackType

# Импорт на верхнем уровне, а не внутри main(): так PyInstaller гарантированно
# видит пакет при анализе зависимостей. При ленивом импорте (внутри функции)
# скрытый импорт пришлось бы дублировать в hiddenimports spec.
from tvhost2.core import diagnostics
from tvhost2.ui.__main__ import main as panel_main


def _install_crash_log() -> None:
    """Записать в журнал ЛЮБОЕ необработанное исключение перед выходом.

    Зачем: сборка идёт с `console=False`, окна консоли нет. Необработанное
    исключение PyInstaller показывает лишь иногда и в модальном окне,
    которое человек закрывает не читая. Итог — «программа не запускается»,
    а журнал пуст.

    Обработчик пишет трейсбек в файл журнала (рядом с .exe) и только затем
    отдаёт управление стандартному обработчику — то есть поведение не
    меняется, но след остаётся.
    """

    def hook(kind: type[BaseException], value: BaseException, tb: TracebackType | None) -> None:
        try:
            text = "".join(traceback.format_exception(kind, value, tb))
            for line in text.strip().splitlines()[-15:]:
                diagnostics.log("err", f"падение: {line}")
            diagnostics.log("err", f"журнал: {diagnostics.JOURNAL.path}")
        except Exception:
            diagnostics.log("err", "обработчик падений не смог записать трейсбек")
        sys.__excepthook__(kind, value, tb)

    sys.excepthook = hook


def main() -> int:
    """Запуск пульта. В сборке --serve включён по умолчанию."""
    _install_crash_log()
    return panel_main()


if __name__ == "__main__":
    sys.exit(main())
