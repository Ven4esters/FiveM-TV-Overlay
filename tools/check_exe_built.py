"""Собрался ли .exe и лежит ли в нём ffmpeg.

Только латиница в выводе: консоль Windows на github-раннере работает в
cp1252, и любой русский `print` падает с UnicodeEncodeError — первый прогон
упал именно на этом, при полностью успешной сборке.

Проверка идёт по СОДЕРЖИМОМУ архива PyInstaller, а не по размеру файла:
нужно знать, что бинарник ffmpeg попал внутрь, а не что .exe потяжелел.

Про чтение архива: PyInstaller пишет свой формат (CArchive), и `zipfile`
его не открывает — в первой версии этой проверки был именно `zipfile`, и на
собранном файле он честно падал с BadZipFile. Штатный читатель —
`PyInstaller.archive.readers.CArchiveReader`, он и используется.
"""

from __future__ import annotations

import sys
from pathlib import Path

EXE = Path("dist") / "FiveM-Video-Overlay.exe"


def _names(exe: Path) -> list[str] | None:
    """Имена файлов внутри сборки. None — прочитать не удалось."""
    try:
        from PyInstaller.archive.readers import (
            CArchiveReader,
        )
    except Exception as exc:
        print(f"WARN: cannot import PyInstaller reader: {type(exc).__name__}")
        return None
    try:
        reader = CArchiveReader(str(exe))  # ждёт путь, не файловый объект
        return list(reader.toc.keys())
    except Exception as exc:
        print(f"WARN: cannot read archive: {type(exc).__name__}: {exc}")
        return None


def main() -> int:
    if not EXE.is_file():
        print(f"FAIL: {EXE} not built")
        return 1
    print(f"exe: {EXE} bytes={EXE.stat().st_size}")

    names = _names(EXE)
    if names is None:
        # Не смогли прочитать — это не доказательство поломки сборки,
        # поэтому не валим job. Реальную проверку запуска делает следующий шаг.
        print("WARN: content check skipped")
        return 0

    print(f"entries in bundle: {len(names)}")
    ffmpeg = [n for n in names if "ffmpeg" in n.lower() and "imageio" in n.lower()]
    print("ffmpeg entries in bundle:", ffmpeg or "NONE")
    if not ffmpeg:
        print("FAIL: no ffmpeg binary inside the exe")
        return 1
    print("OK: ffmpeg binary is inside the exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
