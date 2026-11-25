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

# Импорт на верхнем уровне, а не внутри main(): так PyInstaller гарантированно
# видит пакет при анализе зависимостей. При ленивом импорте (внутри функции)
# скрытый импорт пришлось бы дублировать в hiddenimports spec.
from tvhost2.ui.__main__ import main as panel_main


def main() -> int:
    """Запуск пульта. В сборке --serve включён по умолчанию."""
    return panel_main()


if __name__ == "__main__":
    sys.exit(main())
