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


def _selftest() -> int:
    """Печать сведений о сборке без окна — для проверки .exe в CI.

    Зачем: убедиться, что ffmpeg попал ВНУТРЬ собранного файла, можно только
    спросив у самой сборки. Запускать ради этого окно нельзя: в CI нет
    рабочего стола и неполные Qt-плагины, программа не поднимается — и
    «не ответила» означает что угодно, кроме проверяемого.

    Импорт внутри функции намеренный: `tvhost2.ui` тянет PySide6, а PySide6
    в CI без библиотек падает с ImportError на этапе импорта. Самопроверка
    обязана работать и там, поэтому грузим только `core.ffmpeg_bin`.
    """
    from tvhost2.core.ffmpeg_bin import find_ffmpeg, version

    path = find_ffmpeg() or ""
    print(f"SELFTEST ffmpeg={path or 'NONE'}")
    print(f"SELFTEST version={version()}")
    print(f"SELFTEST frozen={bool(getattr(sys, 'frozen', False))}")
    return 0 if path else 1


def main() -> int:
    """Запуск пульта. В сборке --serve включён по умолчанию."""
    # --selftest не требует ни Qt, ни сети: это проверка содержимого сборки.
    if "--selftest" in sys.argv[1:]:
        return _selftest()

    # Импорт на верхнем уровне, а не внутри main(): так PyInstaller гарантированно
    # видит пакет при анализе зависимостей. При ленивом импорте (внутри функции)
    # скрытый импорт пришлось бы дублировать в hiddenimports spec.
    from tvhost2.core import startup
    from tvhost2.ui.__main__ import main as panel_main

    # Порядок важен и не переставляется:
    # 1) потоки — иначе первый же ответ сервера упадёт на отсутствии stderr;
    # 2) перехват падений — чтобы ошибка ниже попала в журнал;
    # 3) сам пульт.
    startup.ensure_streams()
    startup.install_crash_log()
    return panel_main()


if __name__ == "__main__":
    sys.exit(main())
