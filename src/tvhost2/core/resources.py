"""Где лежат файлы приложения — в исходниках и в собранном .exe.

Зачем отдельный модуль: в обычном запуске `Path(__file__).parent` — это
честный путь к пакету. В сборке PyInstaller (`--onefile`) файлы распакованы
во временную папку, и `__file__` ведёт туда: чтение работает, а запись —
нет, папка удаляется при выходе. А путь «рядом с приложением» наоборот:
во временной папке он бессмысленен, зато `sys.executable` указывает на
настоящий .exe, рядом с которым лежат настройки пользователя и ffmpeg.exe.

Смешивать эти два понятия — самая частая причина, по которой программа
работает из исходников и падает в .exe. Поэтому здесь два имени и разные
функции, а не одно «корень проекта».
"""

from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
"""Собраны ли мы в исполняемый файл. PyInstaller ставит `sys.frozen`."""

HERE = Path(__file__).resolve().parent
"""Папка этого модуля: `core/`. Читать из неё можно всегда."""


def resource(*parts: str) -> Path:
    """Файл внутри пакета `tvhost2` (только чтение).

    Путь считается от КОРНЯ ПАКЕТА, а не от этого файла:
    `resource("web", "static")` даёт `src/tvhost2/web/static` в исходниках
    и `<распаковка>/tvhost2/web/static` в onefile — одна и та же структура,
    потому что `--add-data` кладёт файлы с сохранением путей.

    Почему не от файла и не через `..`: первая версия этой функции
    принимала `"..", "web", "static"`, и в сборке получалось
    `<распаковка>/tvhost2/../web/static`, то есть папка НА УРОВЕНЬ ВЫШЕ
    пакета — пустая. Тест `test_frozen_mode_uses_meipass` поймал это до
    первой сборки; теперь `..` не нужен вовсе.
    """
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", HERE)).joinpath("tvhost2", *parts)
    return HERE.parent.joinpath(*parts)


def app_dir() -> Path:
    """Папка, где приложение имеет право писать и где лежит его .exe.

    В исходниках — корень проекта (там уже живут `videos/`, `ffmpeg.exe`).
    В .exe — папка самого файла: next-to-exe остаётся стабильным между
    запусками, в отличие от временной распаковки.
    """
    if FROZEN:
        return Path(sys.executable).resolve().parent
    # core → tvhost2 → src → корень проекта
    return HERE.parents[2]


__all__ = ["FROZEN", "app_dir", "resource"]
