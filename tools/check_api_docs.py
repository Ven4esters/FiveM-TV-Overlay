"""README не должен обещать маршруты, которых нет, и молчать о существующих.

Зачем: README обещал `GET /commands/<sid>`, а сервер отвечает на `/cmd/<sid>`.
Человек, который писал свой пульт по документации, получал 404 и не понимал
почему. Глазами такое не держится: маршруты добавляются в код и забываются в
документе (проверено — шесть маршрутов отсутствовали, один был с неверным
путём).

Как проверяется: маршруты извлекаются из кода сервера регулярным выражением
по таблицам маршрутов, а из README — по строкам таблицы API. Сравниваются
множества. Регулярное выражение по коду — не идеальный разбор, но список
маршрутов в `server.py` записан плоскими литералами, и это стабильно;
если кто-то начнёт генерировать пути, проверка честно упадёт на расхождении.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "src/tvhost2/web/server.py"


def routes_in_code() -> set[str]:
    """Нормализованные маршруты: путь без параметров запроса."""
    text = SERVER.read_text(encoding="utf-8")
    found: set[str] = set()

    # Таблицы маршрутов: ("/path", self._method) и "/path": self._method
    for path in re.findall(r'\("(/[^"]*)",\s*self\._', text):
        found.add(path)
    for path in re.findall(r'"(/[^"]*)":\s*self\._', text):
        found.add(path)
    # Плоские сравнения: if path == "/new" / path.startswith("/control/")
    for path in re.findall(r'path(?:\.rstrip\("/"\))?\s*==\s*"(/[^"]*)"', text):
        found.add(path)
    for path in re.findall(r'path\.startswith\(\s*"(/[^"]*)"', text):
        found.add(path)
    return {normalise(p) for p in found}


def normalise(path: str) -> str:
    """`/player/<sid>` и `/player/` — один и тот же маршрут.

    Хвост с параметрами (`?since=1`) и угловые скобки (`<sid>`) отбрасываются:
    в коде маршрут записан префиксом, а в README — с подстановкой.
    """
    head = path.split("?", 1)[0].strip()
    head = head.split("<", 1)[0]
    return head.rstrip("/") or "/"


def routes_in_readme(name: str) -> set[str]:
    text = (ROOT / name).read_text(encoding="utf-8")
    rows = re.findall(r"^\|\s*`(?:GET|POST)\s+(/[^`]*?)`", text, re.M)
    return {normalise(p) for p in rows}


def main() -> int:
    code = routes_in_code()
    problems: list[str] = []
    for name in ("README.md", "README.en.md"):
        docs = routes_in_readme(name)
        missing = sorted(docs - code)
        undocumented = sorted(code - docs)
        for path in missing:
            problems.append(f"{name}: обещан маршрут {path}, которого нет в коде")
        for path in undocumented:
            problems.append(f"{name}: маршрут {path} есть в коде, но не описан")
    if problems:
        print("\n".join(problems))
        print(f"\nмаршрутов в коде: {len(code)}")
        return 1
    print(f"маршруты совпадают: {len(code)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
