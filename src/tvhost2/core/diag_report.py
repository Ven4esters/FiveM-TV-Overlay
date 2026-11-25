"""Сбор окружения для отчёта об ошибке.

Зачем отдельный модуль: `core/diagnostics.py` держит журнал событий, но сам
журнал может быть пуст — например, когда сервер не поднялся и писать было
некому. Тогда единственный источник правды — этот снимок: версия, пути,
что установлено, что занято.

Что здесь и почему:

  * `snapshot()` — короткие факты: версия, Python, yt-dlp, ffmpeg.
  * `port_state()` — слушает ли кто-то порт. Половина «прокси выключен»
    объясняется тем, что порт занят второй копией или чужой программой.
  * `probe()` — живой запрос к своему же серверу. Проверяет именно то, что
    делает пульт, и различает «не отвечает», «отвечает не наш» и «ок».
  * `report()` — всё вместе одной строкой для буфера обмена.

Модуль не зависит от Qt и от состояния приложения: его можно вызвать из
любой точки, включая самый ранний старт.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

APP_NAME = "FiveM Video Overlay"
"""Имя приложения в отчёте — чтобы отчёт нельзя было спутать с чужим."""


def _version() -> str:
    """Версия приложения. В сборке она же лежит в имени .exe."""
    try:
        from tvhost2 import __version__  # noqa: PLC0415

        return str(__version__)
    except Exception:
        return "?"


def _frozen() -> bool:
    """Собраны ли в .exe. Определяет, какой путь главный в отчёте."""
    return bool(getattr(sys, "frozen", False))


def _app_dir() -> Path:
    """Папка, где приложение имеет право писать и где лежит .exe."""
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def snapshot() -> dict[str, Any]:
    """Короткие факты о запуске — то, что не меняется во время работы."""
    out: dict[str, Any] = {
        "app": APP_NAME,
        "version": _version(),
        "frozen": "да" if _frozen() else "нет (из исходников)",
        "python": platform.python_version(),
        "system": f"{platform.system()} {platform.release()}",
        "exe": sys.executable,
        "cwd": str(Path.cwd()),
        "app_dir": str(_app_dir()),
        "journal": str(_journal_path()),
        "qt_platform": os.environ.get("QT_QPA_PLATFORM") or "—",
    }
    try:
        import yt_dlp  # noqa: PLC0415

        out["yt-dlp"] = getattr(yt_dlp.version, "__version__", "?")
    except ImportError:
        out["yt-dlp"] = "НЕТ"
    try:
        from tvhost2.core import ffmpeg_bin  # noqa: PLC0415

        out["ffmpeg"] = ffmpeg_bin.find_ffmpeg() or "НЕТ"
    except Exception:
        out["ffmpeg"] = "НЕТ"
    return out


def _journal_path() -> Path:
    """Где лежит файл журнала. Считается из `diagnostics`, чтобы не разойтись."""
    try:
        from tvhost2.core import diagnostics  # noqa: PLC0415

        return diagnostics.JOURNAL.path
    except Exception:
        return Path("videos/cache/overlay.log")


def port_state(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> str:
    """Слушает ли кто-то порт. Текст для отчёта, не исключение.

    Зачем: «прокси: выключен» одинаково выглядит и когда сервер не
    запустился, и когда порт занят чужой программой. Ответ на этот вопрос
    делит проблему пополам.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
        except ConnectionRefusedError:
            return f"{host}:{port} — никто не слушает"
        except TimeoutError:
            return f"{host}:{port} — не отвечает (таймаут {timeout} с)"
        except OSError as exc:
            return f"{host}:{port} — ошибка: {exc}"
    return f"{host}:{port} — слушает"


def probe(port: int, host: str = "127.0.0.1", timeout: float = 3.0) -> str:
    """Живой запрос к своему серверу — мимо системного прокси.

    Возвращает готовую строку: `ok`, `чужой ответ`, `не отвечает: <причина>`.
    Это ровно тот запрос, которым пульт рисует бейдж, поэтому расхождений
    между отчётом и тем, что видит человек, не будет.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://{host}:{port}/"
    try:
        with opener.open(urllib.request.Request(url), timeout=timeout) as response:
            raw = response.read(400)
    except urllib.error.HTTPError as exc:
        return f"не ok: HTTP {exc.code} на {url}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return f"не отвечает: {reason} ({url})"
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return f"чужой ответ на {url}: {raw[:80]!r}"
    if not isinstance(data, dict) or data.get("app") != APP_NAME:
        return f"чужой ответ на {url}: {raw[:80]!r}"
    return f"ok, версия {data.get('version', '?')}"


def proxy_environment() -> str:
    """Какие прокси видит система. Важно: в отчёте они МАСКИРУЮТСЯ.

    Прокси-адрес вида `user:pass@host:port` — это учётные данные, их нельзя
    класть в буфер обмена, откуда они попадут в чат поддержки. Показываем
    только хост и порт.
    """
    try:
        proxies = urllib.request.getproxies()
    except Exception as exc:
        return f"не удалось прочитать: {exc}"
    if not proxies:
        return "нет"
    parts = []
    for scheme, value in sorted(proxies.items()):
        parts.append(f"{scheme}={_mask_proxy(value)}")
    return ", ".join(parts)


def _mask_proxy(value: str) -> str:
    """Адрес прокси без логина и пароля."""
    text = str(value)
    if "@" not in text:
        return text
    scheme, _, rest = text.partition("://")
    tail = rest.rsplit("@", 1)[-1]
    return f"{scheme}://***@{tail}" if scheme else f"***@{tail}"


def journal_tail(limit: int = 40) -> list[str]:
    """Последние строки журнала напрямую из файла.

    Читаем файл, а не память процесса: сервер мог не подняться, но строки
    прошлой сессии и записи точки входа лежат на диске.
    """
    path = _journal_path()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"(журнал недоступен: {path} — {exc})"]
    tail = [line for line in lines if line.strip()][-limit:]
    return tail or [f"(журнал пуст: {path})"]


def report(port: int = 9001) -> str:
    """Полный отчёт одной строкой — то, что уходит в буфер обмена.

    Порядок важен: сверху то, что читает человек первым (версия и вердикт
    по порту), ниже подробности. Формат — «ключ: значение», чтобы отчёт
    можно было глазами сравнить с другим, не разбирая JSON.
    """
    lines = [f"=== {APP_NAME}: отчёт ==="]
    for key, value in snapshot().items():
        lines.append(f"{key}: {value}")
    lines.append(f"порт: {port_state(port)}")
    lines.append(f"проверка: {probe(port)}")
    lines.append(f"системный прокси: {proxy_environment()}")
    lines.append("")
    lines.append(f"--- журнал (последние {40}) ---")
    lines.extend(journal_tail())
    return "\n".join(lines)


__all__ = [
    "APP_NAME",
    "journal_tail",
    "port_state",
    "probe",
    "proxy_environment",
    "report",
    "snapshot",
]
