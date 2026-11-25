"""Журнал: одна строка на событие, читается человеком и машиной.

Зачем свой журнал, а не `logging`: заказчик смотрит журнал глазами в окне
«Журнал» и присылает строку из него. Поэтому у записи фиксированный вид:

    HH:MM:SS [kind] текст key=value key=value

kinds: info, new, req, boot, err, player. По `src=` видно, к какому адресу
относится событие, по `code=` — что ответил источник.

Журнал живёт в файле и не растёт бесконечно: при 4000 строк старые
отрезаются. Файл переживает перезапуск, поэтому причины вчерашнего сбоя
можно посмотреть сегодня.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_VALUE_CHARS = 160
"""Предел длины значения в журнале: адреса CDN длинные, нужен хвост."""

MAX_LINES = 4000
"""Сколько строк держим. 4000 хватает на несколько дней работы."""

def _default_log_path() -> Path:
    """Файл журнала: рядом с приложением, а не в текущем каталоге процесса.

    Почему не `Path("videos/cache/overlay.log")`, как было: относительный
    путь считается от ТЕКУЩЕГО каталога. При двойном клике по .exe это папка
    файла, но из ярлыка, из `от имени администратора` или из проводника с
    другим рабочим каталогом это могла быть совсем другая папка — и журнал
    оказывался не там, где его искали. Хуже: папка могла быть защищена, и
    запись молча падала (`_append_file` глушит OSError).

    В сборке берём папку .exe (`resources.app_dir()`), в исходниках — корень
    проекта. Это то место, которое человек реально видит рядом с программой.
    """
    try:
        from . import resources  # noqa: PLC0415

        return resources.app_dir() / "videos" / "cache" / "overlay.log"
    except Exception:
        return Path("videos/cache/overlay.log")


LOG_PATH = _default_log_path()

KINDS = ("info", "new", "req", "boot", "err", "player")


@dataclass
class Entry:
    """Одна запись журнала."""

    at: float
    kind: str
    text: str
    extra: dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    """Готовая строка из файла прошлой сессии — выводится как есть.

    Без этого поля старые записи получали время текущего запуска, и в окне
    «Журнал» строка выглядела как «00:00:00 [info] 00:04:14 [boot] …».
    """

    def line(self) -> str:
        """Строка в том виде, в котором её видит человек."""
        if self.raw:
            return self.raw
        stamp = datetime.fromtimestamp(self.at).strftime("%H:%M:%S")
        parts = [f"{stamp} [{self.kind}] {self.text}"]
        for key, value in self.extra.items():
            if value is None or value == "":
                continue
            parts.append(f"{key}={_short(value)}")
        return " ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        if self.raw:
            return {"at": self.at, "kind": "file", "text": self.raw}
        return {"at": self.at, "kind": self.kind, "text": self.text, **self.extra}


def _short(value: Any) -> str:
    """Значение без переводов строк и длиннее 160 символов не бывает.

    Адреса CDN длинные; в журнале нужен хвост, по которому видно зеркало,
    а не 300 символов base64.
    """
    text = str(value).replace("\n", " ").replace("\r", " ")
    if len(text) > MAX_VALUE_CHARS:
        return "…" + text[-157:]
    return text


class Journal:
    """Потокобезопасный журнал: пишет в память, файл и отдаёт для окна."""

    def __init__(self, path: Path = LOG_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._memory: list[Entry] = []
        self._load_tail()

    def _load_tail(self) -> None:
        """Подтянуть хвост прошлой сессии — иначе журнал «начинается заново»,
        и вчерашнюю ошибку посмотреть негде."""
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for line in lines[-300:]:
            if line.strip():
                self._memory.append(Entry(at=0, kind="file", text=line, raw=line))

    def log(self, kind: str, text: str, **extra: Any) -> Entry:
        entry = Entry(
            at=time.time(), kind=kind if kind in KINDS else "info", text=text, extra=extra
        )
        with self._lock:
            self._memory.append(entry)
            if len(self._memory) > MAX_LINES:
                del self._memory[: len(self._memory) - MAX_LINES]
            self._append_file(entry)
        return entry

    def _append_file(self, entry: Entry) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # одна строка = одна запись: файл читается глазами и grep-ом
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(entry.line() + "\n")
        except OSError:
            pass  # журнал не должен ломать приложение

    def tail(self, limit: int = 200) -> list[Entry]:
        with self._lock:
            return self._memory[-limit:]

    def tail_text(self, limit: int = 200) -> str:
        return "\n".join(e.line() for e in self.tail(limit))

    def tail_json(self, limit: int = 200) -> list[dict[str, Any]]:
        return [e.as_dict() for e in self.tail(limit)]


JOURNAL = Journal()
"""Один журнал на процесс: и сервер, и интерфейс пишут в него."""


def log(kind: str, text: str, **extra: Any) -> Entry:
    """Короткая форма записи — та, которой пользуется весь код."""
    return JOURNAL.log(kind, text, **extra)


def mark_start() -> None:
    """Черта между запусками. Без неё непонятно, где кончился прошлый."""
    log("boot", "=" * 30 + " запуск " + "=" * 30)


def snapshot() -> dict[str, str]:
    """Что за окружение: версии Python, yt-dlp, ffmpeg, пути."""
    import platform  # noqa: PLC0415
    import sys  # noqa: PLC0415

    out = {
        "python": platform.python_version(),
        "system": f"{platform.system()} {platform.release()}",
        "journal": str(LOG_PATH),
    }
    try:
        import yt_dlp  # noqa: PLC0415

        out["yt-dlp"] = getattr(yt_dlp.version, "__version__", "?")
    except ImportError:
        out["yt-dlp"] = "нет"
    try:
        from . import ffmpeg_bin  # noqa: PLC0415

        out["ffmpeg"] = ffmpeg_bin.find_ffmpeg() or "нет"
    except Exception:
        out["ffmpeg"] = "нет"
    out["python_exec"] = sys.executable
    return out


def short_url(url: str) -> str:
    """Адрес для журнала: домен и хвост, без километров base64."""
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit  # noqa: PLC0415

        parts = urlsplit(url)
        host = parts.hostname or ""
        tail = parts.path.rsplit("/", 1)[-1]
        return f"{host}/…/{tail[-40:]}" if tail else host
    except Exception:
        return url[:80]


def dumps(data: Any) -> str:
    """JSON в одну строку — для extra-полей журнала."""
    try:
        return json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(data)
