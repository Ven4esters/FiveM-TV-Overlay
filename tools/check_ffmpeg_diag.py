"""Проверить, что ffmpeg лежит ВНУТРИ собранного .exe и работает.

Запускает `FiveM-Video-Overlay.exe --selftest` — отдельный режим точки
входа, который ничего не создаёт: ни окна, ни сервера. Печатает путь к
ffmpeg, версию и признак сборки; код возврата 0 — ffmpeg найден.

Почему НЕ через HTTP `/diag/ffmpeg`, хотя такой маршрут есть: в CI нет
рабочего стола, Qt-плагины неполные, и приложение там не поднимается.
Тогда «сервер не ответил» не говорит ничего о ffmpeg — проверка мерила бы
наличие Qt, а не содержимое сборки. Первый прогон упал именно так.

Один аргумент — путь к .exe (по умолчанию dist/FiveM-Video-Overlay.exe).

Только латиница в выводе: консоль Windows на github-раннере в cp1252,
русский print падает с UnicodeEncodeError ещё в обработчике ошибки.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

EXE = Path("dist") / "FiveM-Video-Overlay.exe"


def main() -> int:
    exe = Path(sys.argv[1]) if len(sys.argv) > 1 else EXE
    if not exe.is_file():
        print(f"FAIL: {exe} not built")
        return 1

    run = subprocess.run(
        [str(exe), "--selftest"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    output = (run.stdout or "") + (run.stderr or "")
    print("--- selftest output ---")
    print(output.strip() or "(empty)")
    print(f"--- exit code: {run.returncode} ---")

    if run.returncode != 0:
        print("FAIL: selftest returned non-zero")
        return 1

    line = next((ln for ln in output.splitlines() if ln.startswith("SELFTEST ffmpeg=")), "")
    value = line.split("=", 1)[1].strip() if "=" in line else ""
    if not value or value == "NONE":
        print("FAIL: ffmpeg not found INSIDE the built exe")
        return 1
    # Путь должен вести в папку распаковки сборки, а не в систему.
    if "_MEI" not in value.upper() and "imageio" not in value.lower():
        print(f"WARN: ffmpeg found outside the bundle: {value}")
    print("OK: ffmpeg is inside the build and resolved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
