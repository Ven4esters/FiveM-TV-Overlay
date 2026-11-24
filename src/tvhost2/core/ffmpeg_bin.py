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

_CACHE: dict[str, str | None] = {}

_BUNDLED_NAMES = ("ffmpeg.exe", "ffmpeg")


def _project_root() -> Path:
    """Корень проекта: src/tvhost2/core → три уровня вверх."""
    return Path(__file__).resolve().parents[3]


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

    _CACHE["path"] = found
    return found


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


def python_ok() -> bool:
    """Заглушка для будущих проверок окружения (Python версии)."""
    return sys.version_info >= (3, 10)
