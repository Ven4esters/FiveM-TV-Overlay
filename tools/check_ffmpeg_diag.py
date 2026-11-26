"""Спросить у собранной программы, видит ли она ffmpeg.

Зачем отдельный файл, а не `python -c`: в cmd многострочный код внутри
`-c "..."` ломается на кавычках и переносах, и проверка падает раньше,
чем что-либо проверит.

Только латиница в выводе. Консоль Windows на github-раннере работает в
cp1252: русский `print` падает с UnicodeEncodeError ещё в обработчике
ошибки, и настоящая причина пропадает из журнала. Проверено дважды.

Что считается успехом: `/diag/ffmpeg` ответил `found: true` и `runs: true`.
То есть ffmpeg не просто лежит в сборке, а запускается.
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
        print(f"NO RESPONSE: {type(exc).__name__}: {exc}")
        print("The app did not start, or the route changed.")
        return 1

    print("ANSWER /diag/ffmpeg:", json.dumps(data, ensure_ascii=False, indent=2))
    if not data.get("found"):
        print("FAIL: ffmpeg not found INSIDE the built exe")
        return 1
    if not data.get("runs"):
        print("FAIL: ffmpeg found but does not run")
        return 1
    print("OK: ffmpeg is inside the build and runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
