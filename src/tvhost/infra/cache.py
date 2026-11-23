"""
Локальный кеш видео — лечит протухание прямых ссылок (expire ~6 часов).

Скачивает видео один раз и раздаёт его вечно:
    GET /v/<id>.mp4   — прямой файл (Range поддерживается)

Публичный API (используется proxy.py):
    cache.get_or_fetch(url, progress=None) -> Path   # скачать и вернуть путь
    cache.ensure_async(url: str) -> None             # качать в фоне
    cache.is_cached(url: str) -> bool                # уже скачано?
    cache.pathy_for(name: str) -> Path | None        # найти по имени

Запускается только как часть пакета (python -m tvhost.proxy).
Своей командной строки у модуля нет.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

CACHE_DIR = Path("./videos/cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_inflight: set[str] = set()


def _build_filename(url: str) -> str:
    """Стабильное имя файла по хешу URL (без расширения)."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def pathy_for(name: str) -> Path | None:
    """Найти закешированный файл по имени (перебираем допустимые расширения)."""
    for ext in (".mp4", ".webm", ".m4a", ".mp3", ".ogg", ".wav", ".mov", ".mkv"):
        p = CACHE_DIR / f"{name}{ext}"
        if p.is_file():
            return p
    return None


def is_cached(url: str) -> bool:
    return pathy_for(_build_filename(url)) is not None


def _ext_for(url: str, content_type: str | None) -> str:
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        mapping = {
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/quicktime": ".mov",
            "video/x-matroska": ".mkv",
            "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a",
            "audio/webm": ".webm",
            "audio/ogg": ".ogg",
            "audio/wav": ".wav",
        }
        if ct in mapping:
            return mapping[ct]
        if ct.startswith("video/"):
            return ".mp4"
        if ct.startswith("audio/"):
            return ".m4a"
    m = re.search(r"\.(mp4|webm|mov|m4a|mp3|mkv)(?:$|\?)", url, re.I)
    return f".{m.group(1).lower()}" if m else ".mp4"


def _maybe_fix_container(path: Path) -> tuple[Path, str]:
    """Всякий m4s любит приезжать с байтом 'sidx' в начале — портим mp4. Чиним, если есть ffmpeg."""
    try:
        with path.open("rb") as f:
            head = f.read(12)
    except OSError:
        return path, "application/octet-stream"

    mime = (
        "video/mp4"
        if path.suffix == ".mp4"
        else {
            ".webm": "video/webm",
            ".mov": "video/quicktime",
            ".mkv": "video/x-matroska",
            ".m4a": "audio/mp4",
            ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg",
            ".wav": "audio/wav",
        }.get(path.suffix, "application/octet-stream")
    )

    mp4_boxes = (b"ftyp", b"moov", b"mdat", b"free", b"styp", b"sidx")
    if path.suffix == ".mp4" and head[4:8] not in mp4_boxes:
        # может быть сырое DASH-медиа (m4s) — пробуем ffmpeg -c copy
        from .ffmpeg_bin import find_ffmpeg

        ffmpeg_exe = find_ffmpeg()
        if ffmpeg_exe:
            fixed = path.with_name(path.stem + "_fixed.mp4")
            proc = subprocess.run(
                [
                    ffmpeg_exe,
                    "-y",
                    "-i",
                    str(path),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(fixed),
                ],
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0 and fixed.stat().st_size > 1024 * 100:
                path.unlink(missing_ok=True)
                path = fixed
    return path, mime


def get_or_fetch(url: str, progress=None) -> Path:
    """
    Скачать URL в кеш (если ещё нет) и вернуть локальный путь.
    Эта функция блокирующая — лучше запускать в потоке.
    """
    base = _build_filename(url)
    existing = pathy_for(base)
    if existing:
        return existing

    with _locks_guard:
        lock = _locks.setdefault(base, threading.Lock())

    with lock:
        existing = pathy_for(base)  # пока ждали лок, может уже скачалось
        if existing:
            return existing

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Range": "bytes=0-",  # заставляем отдать 200/206 без спец-логики
            },
        )
        tmp = CACHE_DIR / f".tmp_{base}"
        ext = ".mp4"
        content_type = None
        with urllib.request.urlopen(req, timeout=60) as resp:
            content_type = resp.headers.get("Content-Type")
            ext = _ext_for(url, content_type)
            with tmp.open("wb") as out:
                downloaded = 0
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded)

        if downloaded == 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("пустой ответ от источника")

        final = CACHE_DIR / f"{base}{ext}"
        tmp.rename(final)
        final, _mime = _maybe_fix_container(final)
        return final


def ensure_async(url: str) -> None:
    """Запустить скачивание в фоне, если ещё не качается."""
    base = _build_filename(url)
    with _locks_guard:
        if base in _inflight or pathy_for(base):
            return
        _inflight.add(base)

    def worker() -> None:
        try:
            get_or_fetch(url)
        except Exception as e:
            print(f"[cache] не удалось скачать {base}: {e}", file=sys.stderr)
        finally:
            with _locks_guard:
                _inflight.discard(base)

    threading.Thread(target=worker, daemon=True).start()
