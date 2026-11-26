"""Спросить у собранной программы, видит ли она ffmpeg.

Зачем отдельный файл, а не `python -c`: в cmd многострочный код внутри
`-c "..."` ломается на кавычках и переносах, и проверка падает раньше,
чем что-либо проверит. Первый прогон Windows-сборки упал именно так —
при полностью успешной сборке.

Что считается успехом: маршрут `/diag/ffmpeg` ответил `found: true` и
`runs: true`. То есть ffmpeg не просто лежит в сборке, а запускается.
"""

from __future__ import annotations

import json
import sys
import urllib.request

URL = "http://127.0.0.1:9100/diag/ffmpeg"


def main() -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(URL, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"НЕ ОТВЕЧАЕТ: {type(exc).__name__}: {exc}")
        print("Программа не поднялась или маршрут менялся.")
        return 1

    print("ОТВЕТ /diag/ffmpeg:", json.dumps(data, ensure_ascii=False, indent=2))
    if not data.get("found"):
        print("ПРОВАЛ: ffmpeg не найден ВНУТРИ собранного .exe.")
        return 1
    if not data.get("runs"):
        print("ПРОВАЛ: ffmpeg найден, но не запускается.")
        return 1
    print("УСПЕХ: ffmpeg лежит в сборке и запускается.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
