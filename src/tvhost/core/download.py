#!/usr/bin/env python3
"""
Скачать видео (YouTube и др.) и подготовить его для GTA 5 / GitHub Pages.

Требования:
    Python 3.12
    yt-dlp  : pip install yt-dlp   (или скачайте yt-dlp.exe с github.com/yt-dlp/yt-dlp/releases)
    ffmpeg  : https://ffmpeg.org/download.html  (нужен только для сжатия >90 МБ)

Использование:
    python download_and_prepare.py "https://www.youtube.com/watch?v=VIDEO_ID"
    python download_and_prepare.py "https://www.youtube.com/watch?v=VIDEO_ID" --for-github
    python download_and_prepare.py "https://www.youtube.com/watch?v=VIDEO_ID" --name myclip

Что делает:
1. Скачивает лучшее MP4 до 720p (H.264 + AAC) — именно такой формат
   лучше всего играет в CEF (движке браузера GTA 5) и в модах.
2. Если файл > 90 МБ и указан --for-github — сжимает через ffmpeg
   до лимита GitHub (100 МБ на файл).
3. Кладёт результат в ./videos/ — оттуда его сразу раздаёт server.py
   или можно залить на GitHub Pages.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

VIDEOS_DIR = Path("./videos")
GITHUB_LIMIT_MB = 90  # с запасом до лимита GitHub 100 МБ


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def find_yt_dlp() -> str:
    if shutil.which("yt-dlp"):
        return "yt-dlp"
    if shutil.which("yt-dlp.exe"):
        return "yt-dlp.exe"
    if Path("yt-dlp.exe").exists():
        return str(Path("yt-dlp.exe").resolve())
    if Path("yt-dlp").exists():
        return str(Path("yt-dlp").resolve())
    print("❌ yt-dlp не найден. Установите: pip install yt-dlp", file=sys.stderr)
    sys.exit(1)


def compress_for_github(input_path: Path) -> Path:
    """Сжать видео до ~720p / ~1.5 Мбит/с, чтобы уложиться в 90 МБ."""
    if not shutil.which("ffmpeg"):
        print("⚠️  ffmpeg не найден — пропускаю сжатие. Установите ffmpeg для --for-github.")
        return input_path

    output_path = input_path.with_name(input_path.stem + "_gh.mp4")
    # -crf 28 + ограничение битрейта даёт предсказуемый размер при 720p
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vf",
            "scale=-2:720",
            "-c:v",
            "libx264",
            "-crf",
            "28",
            "-preset",
            "medium",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",  # важно: метаданные в начале файла для стриминга
            str(output_path),
        ]
    )
    if output_path.stat().st_size > GITHUB_LIMIT_MB * 1024 * 1024:
        print(
            f"⚠️  После сжатия файл всё ещё > {GITHUB_LIMIT_MB} МБ. "
            "Обрежьте видео или уменьшите разрешение до 480p."
        )
    else:
        input_path.unlink(missing_ok=True)
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Скачать и подготовить видео для GTA 5")
    parser.add_argument("url", help="Ссылка на видео (YouTube, Vimeo и 1000+ сайтов)")
    parser.add_argument("--name", help="Имя файла без расширения (например: myclip)")
    parser.add_argument(
        "--for-github", action="store_true", help="Сжать до <90 МБ для GitHub Pages"
    )
    parser.add_argument("--max-height", default="720", help="Макс. высота, по умолчанию 720")
    args = parser.parse_args()

    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    yt_dlp = find_yt_dlp()

    template = "%(title).80s [%(id)s].%(ext)s" if not args.name else f"{args.name}.%(ext)s"
    out_template = str(VIDEOS_DIR / template)

    # best mp4 до 720p с видео+аудио в одном файле — идеал для CEF
    format_spec = f"best[ext=mp4][height<={args.max_height}]/best[height<={args.max_height}]/best"

    run(
        [
            yt_dlp,
            "--format",
            format_spec,
            "--merge-output-format",
            "mp4",
            "-o",
            out_template,
            "--no-playlist",
            args.url,
        ]
    )

    # Находим только что скачанный файл
    if args.name:
        downloaded = VIDEOS_DIR / f"{args.name}.mp4"
    else:
        downloaded = max(VIDEOS_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime)

    size_mb = downloaded.stat().st_size / 1024 / 1024
    print(f"\n✅ Скачано: {downloaded} ({size_mb:.1f} МБ)")

    if args.for_github and size_mb > GITHUB_LIMIT_MB:
        print(f"📦 Файл > {GITHUB_LIMIT_MB} МБ — сжимаю для GitHub Pages...")
        downloaded = compress_for_github(downloaded)
        size_mb = downloaded.stat().st_size / 1024 / 1024
        print(f"✅ После сжатия: {downloaded} ({size_mb:.1f} МБ)")

    print("\n🔗 Прямая ссылка (локальный сервер):")
    print(f"   http://127.0.0.1:8000/{downloaded.name}")
    print("\n🔗 Прямая ссылка (GitHub Pages, после загрузки в репозиторий):")
    print(f"   https://USERNAME.github.io/REPO/{downloaded.name}")
    print("\n▶️  Запуск сервера:  python server.py")


if __name__ == "__main__":
    main()
