#!/usr/bin/env python3
"""
GTA 5 Video Server — Python 3.12, только стандартная библиотека.

Запуск:
    python server.py
    python server.py --port 8000 --dir ./videos

Что делает:
- Раздаёт файлы из папки ./videos по прямым ссылкам:
    http://127.0.0.1:8000/video.mp4
    http://127.0.0.1:8000/video.webm
- Поддерживает Range-запросы (206 Partial Content) — без них CEF в GTA 5
  часто не может перематывать и зависает на больших файлах.
- Отдаёт правильные Content-Type и CORS (*), чтобы видео открывалось
  и в браузере игры, и в модах (Media Player V, WreckDTV и т.п.).
- Работает без pip install — только Python 3.12.

Если игра на ЭТОМ ЖЕ ПК — ссылка http://127.0.0.1:8000/... уже подойдёт.
Если нужна публичная ссылка для другого ПК/друга — см. README.md, раздел
«Публичная ссылка через туннель» (ngrok / Cloudflare Tunnel).
"""

from __future__ import annotations

import argparse
import ipaddress
import mimetypes
import re
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Расширения, которые разрешаем отдавать (защита от раздачи чего попало)
ALLOWED_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".mp3", ".wav", ".ogg"}

# Явные MIME-типы (mimetypes на разных ОС может отличаться)
MIME_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
}

CHUNK_SIZE = 1024 * 1024  # 1 МБ — отдаём файл кусками, не грузим целиком в RAM


def get_lan_ip() -> str | None:
    """Определить локальный IP для доступа с других устройств в сети."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ipaddress.ip_address(ip).is_private:
            return ip
    except OSError:
        pass
    return None


class VideoHandler(BaseHTTPRequestHandler):
    server_version = "GTAVideoServer/1.0"
    root_dir: Path = Path("./videos").resolve()

    # --- служебное ---
    def log_message(self, format: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), format % args))

    def _send_headers(self, status: int, headers: dict[str, str]) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        # CORS — важно для CEF/модов и для <video> на github.io странице
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def _resolve_path(self, raw_path: str) -> Path | None:
        """Безопасно сопоставить URL-путь с файлом внутри root_dir."""
        parsed = urllib.parse.urlparse(raw_path)
        decoded = urllib.parse.unquote(parsed.path)

        if decoded in ("/", "/index.html"):
            return None  # покажем список ниже

        # Запрещаем выход за пределы папки (.. , %2e%2e и т.п.)
        relative = decoded.lstrip("/")
        candidate = (self.root_dir / relative).resolve()
        try:
            candidate.relative_to(self.root_dir)
        except ValueError:
            return None

        if candidate.suffix.lower() not in ALLOWED_EXTENSIONS:
            return None
        if not candidate.is_file():
            return None
        return candidate

    def _serve_file(self, file_path: Path, head_only: bool = False) -> None:
        file_size = file_path.stat().st_size
        content_type = MIME_TYPES.get(
            file_path.suffix.lower(),
            mimetypes.guess_type(file_path.name)[0] or "application/octet-stream",
        )

        range_header = self.headers.get("Range")
        range_match = re.match(r"bytes=(\d*)-(\d*)", range_header) if range_header else None

        if range_match:
            start_str, end_str = range_match.groups()
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else file_size - 1

            if start >= file_size or end >= file_size:
                self._send_headers(416, {"Content-Range": f"bytes */{file_size}"})
                return

            length = end - start + 1
            self._send_headers(
                206,
                {
                    "Content-Type": content_type,
                    "Content-Length": str(length),
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                },
            )
            if head_only:
                return

            with file_path.open("rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return

        # Обычный GET без Range
        self._send_headers(
            200,
            {
                "Content-Type": content_type,
                "Content-Length": str(file_size),
            },
        )
        if head_only:
            return

        with file_path.open("rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _serve_index(self) -> None:
        files = sorted(
            p
            for p in self.root_dir.iterdir()
            if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS
        )
        items = (
            "\n".join(
                f'<li><a href="/{urllib.parse.quote(p.name)}">{p.name}</a> '
                f"<small>({p.stat().st_size / 1024 / 1024:.1f} МБ)</small></li>"
                for p in files
            )
            or "<li>Пока нет видео. Положите .mp4/.webm в папку videos/</li>"
        )

        body = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>GTA 5 Video Server</title>
<style>body{{font-family:system-ui;max-width:720px;margin:40px auto;padding:0 16px}}
code{{background:#f0f0f0;padding:2px 6px;border-radius:4px}}</style>
</head><body>
<h1>🎬 GTA 5 Video Server</h1>
<p>Прямые ссылки для телевизора в игре:</p>
<ul>{items}</ul>
<p>Пример: <code>http://127.0.0.1:PORT/video.mp4</code></p>
</body></html>""".encode()
        server_port = getattr(self.server, "server_port", 8000)
        body = body.replace(b"PORT", str(server_port).encode())

        self._send_headers(
            200, {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body))}
        )
        self.wfile.write(body)

    # --- HTTP методы ---
    def do_GET(self) -> None:
        path = self._resolve_path(self.path)
        if path is None:
            # Если путь "/" — показать список, иначе 404
            if urllib.parse.urlparse(self.path).path in ("/", "/index.html"):
                self._serve_index()
            else:
                self._send_headers(404, {"Content-Type": "text/plain"})
                self.wfile.write(b"404 Not Found")
            return
        self._serve_file(path, head_only=False)

    def do_HEAD(self) -> None:
        path = self._resolve_path(self.path)
        if path is None:
            self._send_headers(404, {"Content-Type": "text/plain"})
            return
        self._serve_file(path, head_only=True)

    def do_OPTIONS(self) -> None:
        self._send_headers(204, {})


def main() -> None:
    parser = argparse.ArgumentParser(description="GTA 5 Video Server (Python 3.12)")
    parser.add_argument("--port", type=int, default=8000, help="Порт (по умолчанию 8000)")
    parser.add_argument("--host", default="0.0.0.0", help="Хост (по умолчанию 0.0.0.0)")
    parser.add_argument("--dir", default="./videos", help="Папка с видео")
    args = parser.parse_args()

    root = Path(args.dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    VideoHandler.root_dir = root

    server = ThreadingHTTPServer((args.host, args.port), VideoHandler)

    lan_ip = get_lan_ip()
    print("=" * 60)
    print("🎬 GTA 5 Video Server запущен")
    print(f"   Папка с видео : {root}")
    print(f"   Локально      : http://127.0.0.1:{args.port}/video.mp4")
    if lan_ip:
        print(f"   По сети       : http://{lan_ip}:{args.port}/video.mp4")
    print(f"   Список файлов : http://127.0.0.1:{args.port}/")
    print("   Остановка     : Ctrl+C")
    print("=" * 60)
    print("Совет: если GTA 5 на этом же ПК — используйте 127.0.0.1.")
    print("Если нужна публичная ссылка — запустите ngrok/cloudflared (см. README).")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
