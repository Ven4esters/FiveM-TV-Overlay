"""
diag.py — журнал диагностики: то, чего не видно в обычном логе.

Зачем нужен
-----------
Обычный proxy.log писал только «шумные» маршруты и молчал про /hls — то есть
про ВСЁ, что важно при ошибке воспроизведения. Итог: у пользователя не играет,
а в логе пусто, и причину не найти.

Этот модуль хранит «ленту событий» в памяти:
  • каждый запрос плейлиста и сегмента с кодом ответа и временем;
  • ошибки загрузки с CDN (текст исключения);
  • то, что присылает страница-плеер из игры (код ошибки media/hls.js).

Плюс собирает «снимок окружения»: версия Python, ffmpeg, сеть до CDN,
free-порты — то, что чаще всего и бывает причиной.

Публичный API:
    diag.log(kind, text, **extra)      — записать событие
    diag.recent(limit=300)             — лента для окна «Диагностика»
    diag.mark_start()                  — отметить начало новой попытки
    diag.snapshot()                    — снимок окружения (dict)
    diag.report_text()                 — всё в текст для копирования
"""

from __future__ import annotations

import collections
import os
import platform
import re
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse

MAX_EVENTS = 500
_events: collections.deque = collections.deque(maxlen=MAX_EVENTS)
_lock = threading.Lock()
_started = time.time()

# Файл журнала: события должны пережить перезапуск прокси и падение —
# иначе «журнал» есть только в памяти и пропадает вместе с процессом.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_MAX_FILE_BYTES = 2 * 1024 * 1024


def _restore_from_file(limit: int = 200) -> None:
    """
    Восстановить последние события из файла.

    Без этого после перезапуска прокси окно «Диагностика» было бы пустым,
    хотя файл журнала полон.
    """
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()[-limit:]
    except OSError:
        return
    today = time.strftime("%Y-%m-%d")
    for line in lines:
        try:
            stamp, rest = line.split(" ", 1)
            hh, mm, ss = (int(x) for x in stamp.split(":"))
            t = time.mktime(
                time.strptime(f"{today} {hh:02d}:{mm:02d}:{ss:02d}", "%Y-%m-%d %H:%M:%S")
            )
            kind, text = rest.split("] ", 1)
            kind = kind.lstrip("[")
            _events.append({"t": t, "kind": kind, "text": text[:400], "restored": 1})
        except (ValueError, IndexError):
            continue


def _pick_log_path() -> str:
    """
    Выбрать, куда писать журнал.

    Папка программы может быть только для чтения (Program Files, защищённый
    диск) — тогда молча уходим во временную, иначе журнал просто не появится.
    """
    candidates = [
        os.path.join(BASE_DIR, "diag.log"),
        os.path.join(tempfile.gettempdir(), "gta-video-host-diag.log"),
    ]
    for path in candidates:
        try:
            with open(path, "a", encoding="utf-8"):
                return path
        except OSError:
            continue
    return candidates[-1]  # пишем в temp и не падаем


LOG_PATH = _pick_log_path()
_restore_from_file()


def _file_write(line: str) -> None:
    try:
        # ротация: не даём файлу расти бесконечно
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > _MAX_FILE_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()  # важно: при падении всё уже на диске
    except OSError:
        pass


def log(kind: str, text: str, **extra) -> None:
    """
    Записать событие.

    kind: "req" (запрос), "err" (ошибка), "warn", "info", "player" (из игры)
    """
    extra = {k: (str(v)[:300] if not isinstance(v, (int, float)) else v) for k, v in extra.items()}
    with _lock:
        _events.append({"t": time.time(), "kind": kind, "text": str(text)[:400], **extra})
    stamp = time.strftime("%H:%M:%S")
    tail = " ".join(f"{k}={v}" for k, v in extra.items())
    _file_write(f"{stamp} [{kind}] {str(text)[:400]}" + (f"  {tail}" if tail else ""))


def mark_start() -> None:
    """Отметить начало новой попытки — чтобы отделить одно от другого."""
    log("info", "─" * 40)


def recent(limit: int = 300, since: float = 0) -> list[dict]:
    with _lock:
        rows = [e for e in _events if e["t"] >= since]
    return rows[-limit:]


def file_tail(lines: int = 200) -> str:
    """Последние строки файла журнала (переживают перезапуск)."""
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            return "\n".join(f.read().splitlines()[-lines:])
    except OSError:
        return "(файл журнала пуст)"


def clear() -> None:
    with _lock:
        _events.clear()


# Параметры адреса, которые НИКОГДА не попадают в отчёт: это учётные данные
# или подписанные токены доступа. Отчёт пересылают людям — утечка недопустима.
SECRET_QUERY_KEYS = frozenset(
    {
        "token",
        "sig",
        "signature",
        "key",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "password",
        "passwd",
        "pwd",
        "secret",
        "session",
        "sessionid",
        "sid",
        "access_token",
        "refresh_token",
        "hash",
        "expires",
        "cookie",
        "jwt",
        "bearer",
    }
)


def _safe_query(query: str) -> str:
    """Строка параметров без значений секретных ключей."""
    if not query:
        return ""
    parts = []
    for chunk in query.split("&"):
        name, _, value = chunk.partition("=")
        if name.strip().lower() in SECRET_QUERY_KEYS:
            parts.append(f"{name}=***")
        else:
            parts.append(f"{name}={value[:8]}" if len(value) > 8 else chunk)
    return "&".join(parts)


def short_url(url: str, width: int = 90) -> str:
    """
    URL в читаемом виде: домен + хвост, без длинных токенов.

    Секретные параметры (token, sig, key…) заменяются на «***»: отчёт
    отправляют в переписке, и подписанные ссылки в нём недопустимы.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url[:width]

    path = parts.path
    if len(path) > 40:
        path = "…" + path[-39:]
    out = f"{parts.netloc}{path}"
    if parts.query:
        out += "?" + _safe_query(parts.query)[:24]
    return out[:width]


# ---------------------------------------------------------------- снимок
def _net_check(host: str = "river-3-355.rtbcdn.ru", timeout: float = 4.0) -> str:
    """Может ли программа вообще достучаться до CDN (без HTTP)."""
    try:
        t0 = time.time()
        socket.gethostbyname(host)
        ip_ms = (time.time() - t0) * 1000
        s = socket.create_connection((host, 443), timeout=timeout)
        s.close()
        return f"ok (DNS {ip_ms:.0f} мс, TCP 443 открыт)"
    except socket.gaierror:
        return "DNS не резолвится — нет интернета или блокировка"
    except TimeoutError:
        return "таймаут соединения — firewall/провайдер/антивирус"
    except OSError as e:
        return f"нет соединения: {e}"


def _ffmpeg() -> str:
    try:
        from .ffmpeg_bin import find_ffmpeg

        exe = find_ffmpeg()
        if not exe:
            return "НЕ НАЙДЕН (скачивание сегментов не сработает)"
        try:
            out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=5)
            first = (out.stdout or "").splitlines()[0] if out.stdout else ""
            return f"{exe} — {first[:60]}"
        except (OSError, subprocess.SubprocessError):
            return exe
    except Exception as e:
        return f"ошибка проверки: {e}"


def _port_free(port: int) -> str:
    try:
        with socket.socket() as s:
            s.settimeout(0.5)
            return "свободен" if s.connect_ex(("127.0.0.1", port)) != 0 else "занят"
    except OSError:
        return "?"


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "не определён"


def snapshot() -> dict:
    """Снимок окружения — то, что нужно для разбора проблемы."""
    try:
        import yt_dlp

        ytdlp = getattr(yt_dlp.version, "__version__", "?")
    except Exception:
        ytdlp = "не установлен"

    return {
        "python": f"{platform.python_version()} ({platform.system()} {platform.release()})",
        "машина": f"{platform.machine()}, процессор {os.cpu_count()} ядер",
        "локальный IP": _lan_ip(),
        "порты 9000/9001": f"{_port_free(9000)} / {_port_free(9001)}",
        "ffmpeg": _ffmpeg(),
        "yt-dlp": ytdlp,
        "CDN (сеть)": _net_check(),
        "запущено": time.strftime("%d.%m %H:%M:%S", time.localtime(_started)),
        "событий в журнале": len(_events),
    }


def scrub(text: str) -> str:
    """
    Убрать из произвольного текста секретные параметры ссылок.

    Нужно, потому что ошибки CDN часто содержат полный адрес вместе
    с токеном: `HTTP 403 for https://cdn/x.m3u8?token=...`.
    """
    pattern = r"([?&](?:" + "|".join(sorted(SECRET_QUERY_KEYS)) + r")=)[^&\s]+"
    return re.sub(pattern, r"\1***", text, flags=re.IGNORECASE)


def report_text(limit: int = 300) -> str:
    """Всё для копирования: снимок + лента событий."""
    lines = ["=== СНИМОК ОКРУЖЕНИЯ ==="]
    for k, v in snapshot().items():
        lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("=== ЖУРНАЛ СОБЫТИЙ ===")
    rows = recent(limit)
    if not rows:
        lines.append("(в памяти пусто — показываю файл)")
        lines.append(file_tail(120))
    lines.extend(scrub(_format_event(event)) for event in rows)
    return "\n".join(lines)


def _format_event(event: dict) -> str:
    """Одна строка отчёта: время, метка, текст и дополнительные поля."""
    stamp = time.strftime("%H:%M:%S", time.localtime(event.get("t", 0)))
    mark = {"err": "ОШИБКА ", "warn": "! ", "player": "ИГРА "}.get(event.get("kind", ""), "")
    extra = " ".join(
        f"{name}={value}" for name, value in event.items() if name not in ("t", "kind", "text")
    )
    line = f"{stamp} {mark}{event.get('text', '')}"
    return f"{line}  [{extra}]" if extra else line
