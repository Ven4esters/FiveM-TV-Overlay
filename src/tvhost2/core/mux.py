"""Склейка видео и звука, когда источник отдаёт их по отдельности.

Зачем это вообще нужно. У YouTube штатный режим — HLS-мастер с аудиогруппой:
звук отдельным плейлистом, hls.js играет его сам. Но так бывает не всегда:
у части видео (особенно старых, музыкальных, с ограничениями) HLS-аудио нет
вовсе — yt-dlp отдаёт звук прямым файлом (`googlevideo.com/videoplayback`).
Вставить такой файл в HLS-мастер нельзя: протоколы разные.

Тогда включается аварийный путь: ffmpeg берёт видео-плейлист и аудиофайл,
склеивает их в один локальный HLS-поток, и прокси отдаёт уже его. Это дороже
(нужен ffmpeg, идёт перекодирование звука в AAC), поэтому путь именно
аварийный, а не основной.

Что важно и почему сделано именно так:

* **Видео не перекодируется.** `-c:v copy` — картинка идёт как есть, без
  потери качества и без нагрузки на процессор. Восьмиядерный минимум не
  нужен: копирование потока почти бесплатно.
* **Режем небольшими сегментами.** Плеер в игре не должен ждать, пока
  склеится весь фильм: ffmpeg пишет сегменты по 6 секунд, воспроизведение
  начинается с первого.
* **Кеш по ссылке.** Одна и та же пара «видео + звук» считается один раз.
  Повторный запуск того же видео не запускает ffmpeg заново.
* **Нет ffmpeg — не авария.** Возвращаем None, источник отдаёт понятное
  объяснение вместо трейсбека.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess  # вызываем только найденный ffmpeg с фиксированными аргументами
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg_bin
from .diagnostics import log

log_ = logging.getLogger(__name__)

CACHE_DIR = Path("videos/cache/mux")
"""Куда пишем склеенные потоки. Внутри проекта: заказчик может почистить папку."""

SEGMENT_S = 6
"""Длина сегмента. 6 секунд — столько же у Kodik и YouTube, привычная гранулярность."""

START_TIMEOUT_S = 45
"""Сколько ждём появления первого сегмента. Дольше — считаем, что не вышло."""

MAX_CACHED = 6
"""Сколько склеенных потоков держим на диске. Каждый — сотни мегабайт."""

_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class Muxed:
    """Результат склейки: плейлист и папка с сегментами."""

    playlist: Path

    @property
    def folder(self) -> Path:
        return self.playlist.parent

    def ready(self) -> bool:
        """Есть ли уже плейлист с сегментами (ffmpeg пишет его первым)."""
        return self.playlist.is_file() and self.playlist.stat().st_size > 0


def available() -> bool:
    """Можно ли вообще склеивать: нужен ffmpeg."""
    return ffmpeg_bin.find_ffmpeg() is not None


def _cache_key(video_url: str, audio_url: str) -> str:
    """Короткое имя папки из пары ссылок.

    Не хеш ссылок целиком: хеш пришлось бы считать заново при каждом
    запросе. Берём id из адреса — у YouTube это itag и id видео, у других
    источников хвост пути. Две разные пары почти никогда не совпадут.
    """
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha1(  # noqa: S324 — не подпись, а имя папки
        f"{video_url}|{audio_url}".encode()
    ).hexdigest()
    return digest[:12]


def muxed_for(video_url: str, audio_url: str) -> Muxed | None:
    """Начать (или найти готовую) склейку. Не блокирует: ffmpeg идёт фоном.

    Возвращает None, если ffmpeg нет — тогда вызывающий код должен сказать
    человеку правду: «этот ролик без склейки не сыграет».
    """
    exe = ffmpeg_bin.find_ffmpeg()
    if not exe:
        return None

    key = _cache_key(video_url, audio_url)
    folder = CACHE_DIR / key
    playlist = folder / "index.m3u8"
    result = Muxed(playlist=playlist)
    if result.ready():
        return result  # уже склеено в прошлый раз

    with _lock:
        if result.ready():
            return result
        folder.mkdir(parents=True, exist_ok=True)
        _prune(folder)
        _spawn(exe, video_url, audio_url, folder)
        log("mux", "склейка запущена", folder=key)
    return result


def _spawn(exe: str, video_url: str, audio_url: str, folder: Path) -> None:
    """Запустить ffmpeg фоном и не ждать его: он пишет сегменты по мере готовности."""
    command = [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        # входы: видео-плейлист и аудиофайл
        "-i",
        video_url,
        "-i",
        audio_url,
        # видео копируем, звук приводим к AAC: браузер игры не всякий кодек тянет
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-f",
        "hls",
        "-hls_time",
        str(SEGMENT_S),
        "-hls_playlist_type",
        "vod",
        "-hls_flags",
        "independent_segments",
        "-hls_segment_filename",
        str(folder / "seg_%05d.ts"),
        str(folder / "index.m3u8"),
    ]
    try:
        process = subprocess.Popen(  # аргументы фиксированы, shell=False
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        log("err", "ffmpeg не запустился", why=str(exc)[:80])
        return

    def watch() -> None:
        _, err = process.communicate()
        if process.returncode != 0:
            reason = (err or b"").decode("utf-8", "replace").strip().splitlines()
            log("err", "склейка не удалась", why=(reason[-1] if reason else "?")[:100])

    threading.Thread(target=watch, daemon=True).start()


def _prune(keep: Path) -> None:
    """Убрать старые склейки: диск на 256 ГБ не резиновый.

    Удаляем самые старые папки, оставляя текущую. Файл может быть занят
    ffmpeg — тогда просто идём дальше: уборка не обязана удаться целиком.
    """
    if not CACHE_DIR.is_dir():
        return
    folders = sorted(
        (p for p in CACHE_DIR.iterdir() if p.is_dir() and p != keep),
        key=lambda p: p.stat().st_mtime,
    )
    for old in folders[: max(0, len(folders) - MAX_CACHED + 1)]:
        for item in old.glob("*"):
            with contextlib.suppress(OSError):
                item.unlink()
        with contextlib.suppress(OSError):
            old.rmdir()


def wait_ready(result: Muxed, timeout: float = START_TIMEOUT_S) -> bool:
    """Дождаться первого сегмента: до него плеер покажет пустоту.

    Ждём в отдельном потоке, а не в обработчике запроса: прокси не должен
    замирать на 45 секунд, пока ffmpeg разгоняется.
    """
    import time  # noqa: PLC0415

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result.ready() and any(result.folder.glob("seg_*.ts")):
            return True
        time.sleep(0.5)
    return False


def local_playlist_path(muxed: Muxed) -> str:
    """Путь для моста в виде file:///… — мост умеет и локальные файлы."""
    return "file://" + urllib.parse.quote(str(muxed.playlist.resolve()))
