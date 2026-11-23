"""
ffmpeg_bin.py — поиск ffmpeg без ручной установки.

Порядок поиска:
1. переменная окружения GTA5_FFMPEG (путь к exe)
2. ffmpeg в PATH
3. пакет imageio-ffmpeg из pip (ставится автоматически вместе с зависимостями)
4. локальная папка ./bin/ffmpeg(.exe)

Если ничего не найдено — возвращает None, и вызывающий код решает, что делать.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def find_ffmpeg() -> str | None:
    env = os.environ.get("GTA5_FFMPEG")
    if env and Path(env).is_file():
        return env

    which = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if which:
        return which

    local = Path(__file__).parent / "bin" / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    if local.is_file():
        return str(local)

    try:
        import imageio_ffmpeg  # пакет ставится в .venv установщиком

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def require_ffmpeg() -> str:
    exe = find_ffmpeg()
    if not exe:
        raise RuntimeError(
            "ffmpeg не найден. Установите: pip install imageio-ffmpeg "
            "или положите ffmpeg.exe в папку bin/ рядом с proxy.py"
        )
    return exe


if __name__ == "__main__":
    path = find_ffmpeg()
    print(path or "NOT FOUND")
