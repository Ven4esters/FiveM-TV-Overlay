"""Поиск ffmpeg. Нужен для замера громкости и аварийной склейки.

Почему отдельный модуль: ffmpeg ищется в нескольких местах (рядом с
приложением, в PATH, в пакете imageio-ffmpeg), и делать это в каждом
потребителе — значит получить три разных поведения.

Если ffmpeg нет — приложение работает: громкость не выравнивается,
склейка недоступна. Это не авария, поэтому здесь возвращается None,
а не исключение.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .resources import app_dir

_CACHE: dict[str, str | None] = {}

_BUNDLED_NAMES = ("ffmpeg.exe", "ffmpeg")


def _project_root() -> Path:
    """Папка приложения: рядом с ним кладут ffmpeg.exe.

    В исходниках это корень проекта, в собранном .exe — папка самого файла.
    Без этой разницы собранная программа искала бы ffmpeg во временной
    папке распаковки, где его никогда нет.
    """
    return app_dir()


def find_ffmpeg() -> str | None:
    """Путь к ffmpeg или None. Результат кешируется: поиск не бесплатный."""
    if "path" in _CACHE:
        return _CACHE["path"]

    found: str | None = None

    # 1) рядом с приложением: start.bat кладёт сюда скачанный ffmpeg
    root = _project_root()
    for folder in (root / "bin", root, root / "ffmpeg"):
        for name in _BUNDLED_NAMES:
            candidate = folder / name
            if candidate.is_file():
                found = str(candidate)
                break
        if found:
            break

    # 2) в PATH — обычная установка
    if not found:
        found = shutil.which("ffmpeg")

    # 3) imageio-ffmpeg: ставится без прав администратора и работает
    #    на Windows, где ffmpeg не всегда есть в системе.
    if not found:
        try:
            import imageio_ffmpeg  # noqa: PLC0415

            found = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            found = None

    # 4) В собранном .exe — рядом с распакованным пакетом. PyInstaller кладёт
    #    данные по путям вида `_MEIPASS/imageio_ffmpeg/binaries/ffmpeg-win-…exe`,
    #    и `get_ffmpeg_exe()` иногда их не видит: он ищет ФИКСИРОВАННОЕ имя
    #    (`ffmpeg-win-x86_64-v9.0.2.exe`), а в сборку мог попасть файл другой
    #    версии. Ищем по маске — имя зависит от версии пакета.
    if not found:
        found = _scan_bundle()

    _CACHE["path"] = found
    return found


def _scan_bundle() -> str | None:
    """ffmpeg внутри распакованной сборки PyInstaller — поиском по маске.

    Почему не полагаемся только на `imageio_ffmpeg.get_ffmpeg_exe()`: он
    сверяет файл с ЖЁСТКО заданным именем для платформы (например
    `ffmpeg-win-x86_64-v9.0.2.exe`). Если в сборку попала другая версия или
    файл переименован, поиск молча возвращает «нет», и человек видит «замер
    не удался», хотя ffmpeg лежит рядом.
    """
    roots: list[Path] = []
    bundle = getattr(sys, "_MEIPASS", "")  # папка распаковки --onefile
    if bundle:
        roots.append(Path(bundle))
    roots.append(app_dir())
    for root in roots:
        for folder in (root / "imageio_ffmpeg" / "binaries", root / "bin", root):
            if not folder.is_dir():
                continue
            for pattern in ("ffmpeg*.exe", "ffmpeg-*", "ffmpeg"):
                for candidate in sorted(folder.glob(pattern)):
                    if candidate.is_file() and candidate.name != "ffmpeg_bin.py":
                        return str(candidate)
    return None


def require_ffmpeg() -> str:
    """То же, но для мест, где без ffmpeg нельзя (склейка дорожек)."""
    path = find_ffmpeg()
    if not path:
        raise RuntimeError(
            "ffmpeg не найден. Положите ffmpeg.exe рядом с приложением "
            "или установите его в систему."
        )
    return path


def version() -> str:
    """Версия для журнала при запуске."""
    path = find_ffmpeg()
    if not path:
        return "нет"
    import subprocess  # noqa: PLC0415

    try:
        out = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        first = (out.stdout or "").splitlines()[0]
        return first.replace("ffmpeg version ", "")[:40] if first else "?"
    except (OSError, subprocess.SubprocessError, IndexError):
        return "?"
