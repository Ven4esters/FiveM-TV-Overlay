"""
pins.py — «вечные» ссылки для потоков, которые живут только в сессии браузера.

Проблема
--------
Сайты кинотеатров отдают подписанные .m3u8: адрес содержит токен, срок
жизни ~30–60 минут, а иногда он привязан и к cookie/заголовкам браузера.
Скопировать такой адрес в телевизор можно, но через полчаса он умирает —
и при каждом переключении серии всё повторяется.

Решение (как работает «закрепление»)
------------------------------------
1. В момент, когда ссылка ещё жива, мы запоминаем ВСЁ, что нужно, чтобы она
   работала: сам адрес, Referer, Origin, User-Agent и cookie из вашего браузера.
   Это и есть запись в pins.json — «сессия» ссылки.
2. В фоне ссылка скачивается один раз в локальный файл (videos/cache/…mp4).
3. Наружу мы отдаём свою короткую ссылку вида  http://<ip>:9000/v/<id>.mp4
   Она не протухает никогда: файл лежит у вас на диске.

Если файл ещё качается — /v/<id> не падает, а сразу начинает отдавать
байты по мере готовности (Range работает), поэтому телевизор можно включать
сразу после нажатия «Закрепить».

Публичный API:
    pins.add(url, referer="", origin="", user_agent="", cookies="", title="") -> dict
    pins.get(pid) -> dict | None
    pins.all() -> list[dict]
    pins.remove(pid) -> bool
    pins.prepare(pid) -> dict   # запустить скачивание (в фоне)
    pins.ready_path(pid) -> Path | None
    pins.log_for(pid) -> str
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "videos" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
PINS_PATH = BASE_DIR / "videos" / "pins.json"

USER_AGENT_DEFAULT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_lock = threading.Lock()
_pins: dict[str, dict] = {}
_downloading: set[str] = set()
_procs: dict[str, subprocess.Popen] = {}
_logs: dict[str, list[str]] = {}

MAX_LOG = 200


# ---------------------------------------------------------------- хранилище
def _load() -> None:
    global _pins
    try:
        raw = json.loads(PINS_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            _pins = raw
    except (OSError, ValueError):
        _pins = {}


def _save() -> None:
    try:
        PINS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PINS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_pins, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(PINS_PATH)
    except OSError:
        pass


def _pid_for(url: str) -> str:
    """
    Короткий стабильный id: одинаковый URL → одна и та же запись.

    sha256, а не sha1: sha1 считается нестойким, и статические анализаторы
    справедливо ругаются. Здесь хеш — ключ, а не защита, но исключений
    быть не должно: правило одно для всего проекта.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- API
def add(
    url: str,
    referer: str = "",
    origin: str = "",
    user_agent: str = "",
    cookies: str = "",
    title: str = "",
) -> dict:
    """Запомнить ссылку вместе с контекстом браузера (сессией)."""
    pid = _pid_for(url)
    with _lock:
        rec = _pins.get(pid) or {}
        rec.update(
            {
                "id": pid,
                "url": url,
                "referer": referer or rec.get("referer", ""),
                "origin": origin or rec.get("origin", ""),
                "user_agent": user_agent or rec.get("user_agent", USER_AGENT_DEFAULT),
                "cookies": cookies or rec.get("cookies", ""),
                "title": title or rec.get("title", ""),
                "created": rec.get("created") or time.time(),
                "state": rec.get("state") or "new",  # new|downloading|ready|error
                "progress": rec.get("progress") or 0,
                "size": rec.get("size") or 0,
                "file": rec.get("file") or "",
                "error": rec.get("error") or "",
            }
        )
        _pins[pid] = rec
        _save()
        return dict(rec)


def get(pid: str) -> dict | None:
    with _lock:
        rec = _pins.get(pid)
        return dict(rec) if rec else None


def all() -> list[dict]:
    with _lock:
        rows = sorted(_pins.values(), key=lambda r: r.get("created") or 0, reverse=True)
        return [dict(r) for r in rows]


def remove(pid: str) -> bool:
    stop(pid)
    with _lock:
        rec = _pins.pop(pid, None)
        if rec:
            f = rec.get("file")
            if f:
                try:
                    (CACHE_DIR / f).unlink(missing_ok=True)
                except OSError:
                    pass
            _save()
    return bool(rec)


def _update(pid: str, **kw) -> None:
    with _lock:
        rec = _pins.get(pid)
        if not rec:
            return
        rec.update(kw)
        _save()


def _log(pid: str, line: str) -> None:
    with _lock:
        buf = _logs.setdefault(pid, [])
        buf.append(time.strftime("%H:%M:%S ") + line)
        del buf[:-MAX_LOG]


def log_for(pid: str) -> str:
    with _lock:
        return "\n".join(_logs.get(pid) or [])


VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".mkv", ".m4a", ".mp3")


def _file_ext(url: str) -> str:
    """
    Расширение выходного файла по адресу источника.

    Смотрим ТОЛЬКО путь, без строки параметров: в адресах вида
    `/stream?file=a.webm` расширение из параметра не должно влиять на имя
    файла — иначе mp4-поток сохранится как .webm и не откроется на телевизоре.
    """
    path = urllib.parse.urlsplit(url).path.lower()
    for ext in VIDEO_EXTENSIONS:
        if path.endswith(ext):
            return ext
    return ".mp4"


def ready_path(pid: str) -> Path | None:
    rec = get(pid)
    if not rec or rec.get("state") != "ready":
        return None
    f = rec.get("file") or ""
    p = CACHE_DIR / f if f else None
    return p if p and p.is_file() else None


def headers_for(rec: dict) -> dict:
    """Заголовки, с которыми ссылка отдавалась в браузере пользователя."""
    h = {"User-Agent": rec.get("user_agent") or USER_AGENT_DEFAULT, "Accept": "*/*"}
    if rec.get("referer"):
        h["Referer"] = rec["referer"]
    if rec.get("origin"):
        h["Origin"] = rec["origin"]
    elif rec.get("referer"):
        try:
            u = urllib.parse.urlsplit(rec["referer"])
            h["Origin"] = f"{u.scheme}://{u.netloc}"
        except ValueError:
            pass
    if rec.get("cookies"):
        h["Cookie"] = rec["cookies"]
    return h


# ---------------------------------------------------------------- скачивание
def prepare(pid: str) -> dict:
    """Начать загрузку закреплённой ссылки в локальный файл (идемпотентно)."""
    rec = get(pid)
    if not rec:
        return {"ok": False, "error": "запись не найдена"}
    if rec.get("state") == "ready" and ready_path(pid):
        return {"ok": True, "state": "ready"}
    with _lock:
        if pid in _downloading:
            return {"ok": True, "state": "downloading"}
        _downloading.add(pid)
    _update(pid, state="downloading", error="", progress=0)
    threading.Thread(target=_download_worker, args=(pid,), daemon=True).start()
    return {"ok": True, "state": "downloading"}


def stop(pid: str) -> None:
    with _lock:
        proc = _procs.pop(pid, None)
    if proc is not None:
        try:
            proc.kill()
        except OSError:
            pass
        _log(pid, "остановлено пользователем")


MIN_PINNED_BYTES_FFMPEG = 50_000
MIN_PINNED_BYTES_HTTP = 10_000


def _download_worker(pid: str) -> None:
    """
    Скачать закреплённую ссылку в локальный файл.

    Два пути: ffmpeg (умеет HLS/DASH и склеивает в mp4) и обычный HTTP
    для прямых .mp4. Каждый путь — отдельная функция.
    """
    record = get(pid)
    if not record:
        with _lock:
            _downloading.discard(pid)
        return

    url = record["url"]
    out = CACHE_DIR / f"pin_{pid}{_file_ext(url)}"
    tmp = CACHE_DIR / f".pin_{pid}.part"
    tmp.unlink(missing_ok=True)
    headers = headers_for(record)

    if _try_ffmpeg(pid, url, headers, tmp, out):
        return
    _try_plain_http(pid, url, headers, tmp, out)


def _try_ffmpeg(pid: str, url: str, headers: dict, tmp: Path, out: Path) -> bool:
    """Скачать через ffmpeg. True — файл готов и запись обновлена."""
    from ..infra.ffmpeg_bin import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False

    # HLS/DASH и всё, что ffmpeg умеет сам: он же и склеивает в mp4
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-user_agent",
        headers["User-Agent"],
    ]
    if headers.get("Referer"):
        cmd += ["-referer", headers["Referer"]]
    if headers.get("Cookie"):
        cmd += ["-headers", f"Cookie: {headers['Cookie']}\r\n"]
    cmd += ["-i", url, "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp)]
    _log(pid, f"ffmpeg: скачиваю {url[:80]}…")

    error = _run_ffmpeg_with_progress(pid, cmd, tmp)
    if tmp.exists() and tmp.stat().st_size > MIN_PINNED_BYTES_FFMPEG:
        out.unlink(missing_ok=True)
        tmp.replace(out)
        _finish_ready(pid, out)
        return True

    tail = (error or "").strip().splitlines()[-4:]
    _log(pid, "ffmpeg не смог: " + ("; ".join(tail) or "неизвестно"))
    return False


def _run_ffmpeg_with_progress(pid: str, cmd: list[str], tmp: Path) -> str:
    """Запустить ffmpeg и обновлять размер файла, пока он качается."""
    error = ""
    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        with _lock:
            _procs[pid] = process
        last_update = 0.0
        while process.poll() is None:
            if time.time() - last_update > 1.5:
                last_update = time.time()
                size = tmp.stat().st_size if tmp.exists() else 0
                _update(pid, size=size)
            time.sleep(0.3)
        error = (process.stderr.read() if process.stderr else "") or ""
        with _lock:
            _procs.pop(pid, None)
    except OSError as exc:
        error = str(exc)
    finally:
        with _lock:
            _procs.pop(pid, None)
    return error


def _try_plain_http(pid: str, url: str, headers: dict, tmp: Path, out: Path) -> None:
    """Запасной путь без ffmpeg — скачать как файл (для прямых .mp4)."""
    _log(pid, "скачиваю напрямую через HTTP…")
    try:
        _fetch_to_file(pid, url, headers, tmp)
        if tmp.stat().st_size < MIN_PINNED_BYTES_HTTP:
            raise RuntimeError("файл слишком мал — похоже, это не видео")
        out.unlink(missing_ok=True)
        tmp.replace(out)
        _finish_ready(pid, out)
    except Exception as exc:
        _log(pid, f"ошибка: {exc}")
        tmp.unlink(missing_ok=True)
        _update(pid, state="error", error=str(exc)[:300], progress=0)
    finally:
        with _lock:
            _downloading.discard(pid)


def _fetch_to_file(pid: str, url: str, headers: dict, tmp: Path) -> None:
    """Скачать URL в файл, обновляя прогресс записи."""
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        received = 0
        last_update = 0.0
        with tmp.open("wb") as stream:
            while chunk := response.read(1024 * 256):
                stream.write(chunk)
                received += len(chunk)
                if time.time() - last_update > 0.7:
                    last_update = time.time()
                    percent = int(received * 100 / total) if total else 0
                    _update(pid, size=received, progress=percent)


def _finish_ready(pid: str, path: Path) -> None:
    size = path.stat().st_size
    _log(pid, f"готово: {path.name} ({size / 1024 / 1024:.0f} МБ)")
    _update(pid, state="ready", file=path.name, size=size, progress=100, error="")
    with _lock:
        _downloading.discard(pid)


VIDEO_SERVE_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".m4a", ".mp3")
SERVE_CHUNK_BYTES = 1024 * 512


def _strip_extension(pid: str) -> str:
    """Убрать расширение видео из идентификатора: /v/<id>.mp4 → <id>."""
    lowered = pid.lower()
    for ext in VIDEO_SERVE_EXTS:
        if lowered.endswith(ext):
            return pid[: -len(ext)]
    return pid


def _pinned_file(pid: str) -> Path | None:
    """Файл закрепления: готовый или ещё качающийся (.part)."""
    record = get(pid)
    if not record:
        return None
    name = record.get("file") or ""
    path = CACHE_DIR / name if name else None
    if path and path.is_file():
        return path
    part = CACHE_DIR / f".pin_{pid}.part"
    return part if part.is_file() else None


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """
    Разобрать заголовок Range в (начало, конец) включительно.

    Понимает все три формы из RFC 7233:
        bytes=100-200   обычный диапазон
        bytes=100-      от 100 до конца файла
        bytes=-500      ПОСЛЕДНИЕ 500 байт (не «от 0 до 500»!)

    Возвращает None, если заголовка нет (отдаём файл целиком).
    Бросает ValueError, если заголовок есть, но выполнить его нельзя —
    вызывающий обязан ответить 416 (RFC 7233, §4.4).

    Форму «последние N байт» плееры используют, чтобы прочитать хвост
    MP4 (moov-atom) до начала воспроизведения. Если её перепутать с
    «от 0 до N», плеер получает начало файла вместо индекса и видео
    либо не стартует, либо прыгает в начало при перемотке.
    """
    if not header:
        return None  # заголовка нет — отдаём файл целиком

    match = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header)
    if not match:
        raise ValueError("непонятный Range")  # заголовок есть, но битый → 416
    first, last = match.group(1), match.group(2)

    # «bytes=-500» — суффиксный диапазон: последние 500 байт
    if not first and last:
        length = int(last)
        if length <= 0 or size <= 0:
            raise ValueError("пустой суффиксный диапазон")
        return max(size - length, 0), size - 1

    # «bytes=-» — мусор без чисел
    if not first and not last:
        raise ValueError("диапазон без чисел")

    start = int(first)
    if start >= size:
        raise ValueError("начало за пределами файла")  # → 416
    end = int(last) if last else size - 1
    if end < start:
        raise ValueError("перевёрнутый диапазон")  # → 416
    return start, min(end, size - 1)


def _send_file_headers(handler, size: int, mime: str, byte_range: tuple[int, int] | None) -> bool:
    """
    Отправить заголовки ответа.

    Возвращает False, если запрошенный диапазон невозможен (416).
    """
    if byte_range is None:
        handler.send_response(200)
        handler.send_header("Content-Type", mime)
        handler.send_header("Content-Length", str(size))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        return True

    start, end = byte_range
    if start > end or start >= size:
        handler.send_response(416)
        handler.send_header("Content-Range", f"bytes */{size}")
        handler.end_headers()
        return False

    handler.send_response(206)
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(end - start + 1))
    handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    return True


def _pump_file(handler, path: Path, byte_range: tuple[int, int] | None) -> None:
    """Перекачать файл (или его часть) клиенту порциями."""
    start = byte_range[0] if byte_range else 0
    left = (byte_range[1] - start + 1) if byte_range else None
    with path.open("rb") as stream:
        stream.seek(start)
        while True:
            wanted = SERVE_CHUNK_BYTES if left is None else min(SERVE_CHUNK_BYTES, left)
            chunk = stream.read(wanted)
            if not chunk:
                return
            try:
                handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return  # клиент закрыл соединение — норма при перемотке
            if left is not None:
                left -= len(chunk)
                if left <= 0:
                    return


def serve_partial(handler, pid: str) -> bool:
    """
    Отдать /v/<pid> даже пока файл качается: Range по уже записанным байтам.

    Возвращает False, если отдавать нечего (нет файла) — тогда вызывающий
    код отдаёт понятную страницу.

    Разбито на шаги: найти файл, разобрать Range, отправить заголовки,
    перекачать тело. Раньше это был один блок со сложностью 16.
    """
    pid = _strip_extension(pid.split("?", maxsplit=1)[0].strip("/"))
    path = _pinned_file(pid)
    if path is None:
        return False

    size = path.stat().st_size
    if size <= 0:
        return False

    try:
        byte_range = _parse_range(handler.headers.get("Range"), size)
    except ValueError:
        # Заголовок есть, но диапазон невыполним — по стандарту 416,
        # а не «отдадим весь файл»: плеер иначе считает размер неверно.
        handler.send_response(416)
        handler.send_header("Content-Range", f"bytes */{size}")
        handler.end_headers()
        return True
    if not _send_file_headers(handler, size, "video/mp4", byte_range):
        return True  # 416 уже отправлен
    if handler.command == "HEAD":
        return True

    _pump_file(handler, path, byte_range)
    return True


_load()
