"""
proxy.py — превращает «страничные» ссылки с видео в прямые ссылки для GTA 5.

Зачем: телевизор в GTA 5 (CEF) понимает только прямые ссылки на MP4/WebM.
YouTube, Vimeo, Rutube, VK и тысячи других сайтов отдают страницу, а не файл.
Этот сервер принимает обычную ссылку и возвращает прямую.

Запуск:
    pip install yt-dlp
    python proxy.py                 # http://127.0.0.1:9000

Как пользоваться в игре:
    Обычная ссылка : https://www.youtube.com/watch?v=XXXX
    Прямая для GTA : http://127.0.0.1:9000/yt?url=https://www.youtube.com/watch?v=XXXX

    http://127.0.0.1:9000/<ID>          — короткая постоянная ссылка (из /list)
    http://127.0.0.1:9000/              — список всех видео с готовыми ссылками
    http://127.0.0.1:9000/yt?url=...    — резолв YouTube (через локальный кеш)
    http://127.0.0.1:9000/r?url=...     — резолв «любого» сайта (через кеш)
    http://127.0.0.1:9000/get?url=...   — отдать CEF как есть, если сервер уже
                                          отдаёт настоящий файл с Range

Как это связано с CEF:
    1. m3u8 (HLS) и DASH — НЕ воспроизводятся в CEF → конвертируем в MP4/WebM
       через ffmpeg (-c copy, без перекодирования — быстро).
    2. googlevideo-ссылки YouTube живут ~6 ч и требуют заголовков →
       YouTube всегда отдаём через /yt (локальный кеш, вечная ссылка).
    3. Другие сайты с настоящим файлом (NASA, архив ИИ, Wikimedia и т.п.)
       отдаём потоком через /get — без скачивания.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import pins
from .infra import cache, sources
from .infra import diagnostics as diag
from .web import player as player_mod

# Заголовки для запросов к источникам.
# ВАЖНО (проверено на w3.org за Cloudflare): поддельный Chrome-UA без ПОЛНОГО
# набора sec-ch-ua-* заголовков блокируется челленджем (403), а Firefox-UA или
# отсутствие UA — проходят. Поэтому используем Firefox-отпечаток — он согласован.
FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"
USER_AGENT = FIREFOX_UA

# Сайты, которые физически не отдадут видео без логина и DRM-модуля.
# Для них сразу объясняем причину, не тратя 40 секунд на таймауты.
LOGIN_WALLED = {
    "kinopoisk.ru": "Кинопоиск требует подписку и вход через Яндекс; видео "
    "защищено DRM — сторонний плеер его не воспроизведёт",
    "hd.kinopoisk.ru": "Кинопоиск HD — нужна подписка, вход и DRM",
    "netflix.com": "Netflix — DRM (Widevine), воспроизведение вне приложения невозможно",
    "disneyplus.com": "Disney+ — DRM, воспроизведение вне приложения невозможно",
    "ivi.ru": "IVI — нужна авторизация и DRM",
    "okko.tv": "Okko — нужна подписка и DRM",
    "wink.ru": "Wink — нужна подписка и DRM",
    "more.tv": "More.tv — нужна подписка и DRM",
    "premier.one": "Premier — нужна подписка и DRM",
    "start.ru": "Start — нужна подписка и DRM",
    "amediateka.ru": "Амедиатека — нужна подписка и DRM",
    "vk.com/video": "VK Видео: часть роликов закрыта, но открытые обычно работают",
}


def is_login_walled(url: str) -> tuple[bool, str]:
    """Проверить, относится ли ссылка к заведомо закрытому сервису."""
    low = (url or "").lower()
    for needle, reason in LOGIN_WALLED.items():
        if needle in low:
            # VK — исключение: у него есть открытые ролики, пробуем
            if needle == "vk.com/video":
                return False, ""
            return True, reason
    return False, ""


YT_SOCKET_TIMEOUT = 12  # секунд ожидания сокета для yt-dlp
RESOLVE_TIMEOUT = 40  # общий потолок на разбор одной ссылки

# Некоторые CDN (VK/okcdn, YouTube) отвергают Firefox-UA и требуют Chrome.
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
CHROME_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
}

# Домены, которым нужен именно Chrome (иначе 400/502)
_NEEDS_CHROME = (
    "okcdn.ru",
    "vkvideo.ru",
    "vk.com",
    "googlevideo.com",
    "manifest.googlevideo.com",
    "akamaized.net",
)

BROWSER_HEADERS = {
    "User-Agent": FIREFOX_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,video/*;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3",
    "Accept-Encoding": "identity",
    "Sec-Fetch-Dest": "video",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
    "Upgrade-Insecure-Requests": "1",
}

VIDEO_EXT_RE = re.compile(r"\.(mp4|webm|mov|m4v|mkv)(?:$|\?)", re.I)
PLAY_ID_RE = re.compile(r"^/p/([a-z0-9]{6,})$", re.I)

# локальный реестр «обычная ссылка → короткий id» (переживает перезапуск)
BASE_DIR = Path(__file__).resolve().parent
REGISTRY_PATH = Path("./videos/registry.json")
REGISTRY: dict[str, str] = {}  # id -> url
PLAYER_SESSIONS = player_mod.SessionStore()  # сессии страницы-плеера
_ffmpeg_procs: set[subprocess.Popen] = set()


def load_registry() -> None:
    global REGISTRY
    try:
        REGISTRY = json.loads(REGISTRY_PATH.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        REGISTRY = {}


def save_registry() -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(REGISTRY, ensure_ascii=False, indent=1), "utf-8")


def register_url(url: str) -> str:
    for rid, u in REGISTRY.items():
        if u == url:
            return rid
    import hashlib

    rid = hashlib.sha256(url.encode()).hexdigest()[:8]
    REGISTRY[rid] = url
    save_registry()
    return rid


def is_probably_direct(url: str) -> tuple[bool, bool]:
    """Вернуть (похоже_на_прямой_файл, это_HLS)."""
    low = (url or "").lower()
    hls = ".m3u8" in low
    direct = bool(VIDEO_EXT_RE.search(low)) and ".m3u8" not in low
    return direct, hls


def safe_int(value: object, default: int = 0) -> int:
    """
    Привести к int что угодно, не падая.

    Раньше `int(qs["since"][0])` и `int(headers["Content-Length"])` падали
    на мусоре: сервер отдавал 500 и закрывал соединение вместо ответа 400.
    Значение приходит из сети, доверять ему нельзя (ERR-054).
    """
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


def probe_content_type(url: str) -> tuple[str | None, int | None]:
    req = urllib.request.Request(url, method="HEAD", headers=dict(BROWSER_HEADERS))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            length = resp.headers.get("Content-Length")
            return resp.headers.get("Content-Type"), int(length) if length else None
    except Exception:
        return None, None


def yt_formats(url: str) -> list[dict]:
    """Список форматов yt-dlp (плоский, без скачивания)."""
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp не установлен: pip install yt-dlp")

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "format": "all",
        # Жёсткие таймауты: без них yt-dlp может висеть минутами на живых
        # страницах (Кинопоиск, VK, сайты с антиботом) — и пульт «зависает».
        "socket_timeout": YT_SOCKET_TIMEOUT,
        "retries": 1,
        "extractor_retries": 1,
        "fragment_retries": 1,
        "extractor_args": {"youtube": {"player_client": ["web"]}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    fmts = info.get("formats") or []
    return [
        {
            "id": f.get("format_id"),
            "ext": f.get("ext"),
            "height": f.get("height"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "tbr": f.get("tbr"),
            "fps": f.get("fps"),
            "filesize": f.get("filesize"),
            "url": f.get("url"),
        }
        for f in fmts
        if f.get("url")
    ]


def pick_direct(fmts: list[dict]) -> str | None:
    """Выбрать одну готовую ссылку: h264 mp4 до 720p с аудио, иначе лучшее progressive."""

    def score(f: dict) -> tuple:
        progressive = f["vcodec"] != "none" and f["acodec"] != "none"
        h264 = (f["vcodec"] or "").startswith("avc")
        h = f["height"] or 0
        return (progressive, h264, h <= 720, h)

    candidates = [f for f in fmts if f["vcodec"] != "none" and f["acodec"] != "none"]
    if not candidates:
        return None
    best = max(candidates, key=score)
    return best["url"]


def test_url(url: str, headers: dict | None = None) -> bool:
    """Проверить, что CEF сможет это открыть: 200/206 + video/*."""
    h = dict(BROWSER_HEADERS)
    h["Range"] = "bytes=0-1023"
    if headers:
        h.update(headers)
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=20) as resp:
            content_type = resp.headers.get("Content-Type") or ""
            return resp.status in (200, 206) and content_type.startswith("video/")
    except Exception:
        return False


# Сайты, которые прокси умеет разбирать своими путями (yt-dlp и т.п.).
# Их нельзя перехватывать универсальным поиском потока: он вытащит
# неполный набор дорожек (например, только видео без звука).
KNOWN_MEDIA_HOSTS = (
    "rutube.ru",
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "vk.com",
    "vkvideo.ru",
    "vk.ru",
    "ok.ru",
    "dailymotion.com",
    "dai.ly",
    "vimeo.com",
    "twitch.tv",
    "ttvnw.net",
    "kodikplayer.com",
    "kodik.info",
    "animego.tv",
    "animego.me",
    "animego.org",
    "animego.one",
)


def _is_rutube(url: str) -> bool:
    """Rutube разбирается отдельной веткой (видео и звук одной дорожкой)."""
    return sources.is_rutube(url)


def _is_known_media_host(url: str) -> bool:
    """Сайт из списка тех, что разбираются штатными путями."""
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in KNOWN_MEDIA_HOSTS)


def resolve_input(url: str) -> dict:
    """
    Обычная ссылка → данные для прямой ссылки.
    Возвращает dict с полями: kind=("direct"|"hls"|"page"), url, formats.
    """
    direct, hls = is_probably_direct(url)
    if direct:
        ctype, size = probe_content_type(url)
        if ctype and ctype.startswith(("video/", "audio/", "application/octet-stream")):
            return {
                "kind": "direct",
                "url": url,
                "content_type": ctype,
                "size": size,
                "verified": True,
            }
        # некоторые CDN режут HEAD/пробные запросы (Cloudflare и т.п.),
        # но отдают видео при реальном GET — такой URL всё равно считаем прямым
        return {
            "kind": "direct",
            "url": url,
            "content_type": "video/mp4",
            "size": size,
            "verified": test_url(url),
        }
    if hls:
        return {"kind": "hls", "url": url, "content_type": "application/vnd.apple.mpegurl"}

    fmts = yt_formats(url)
    direct_url = pick_direct(fmts)
    if direct_url:
        return {"kind": "direct", "url": direct_url, "formats": fmts}
    return {"kind": "page", "formats": fmts}


def build_yt_master(url: str) -> str | None:
    """
    YouTube: HLS отдаётся отдельными дорожками — видео и звук.
    Собираем master: по одному видео-потоку на высоту + аудио-группа.
    Тогда hls.js играет напрямую, без скачивания и склейки.
    """
    try:
        import yt_dlp
    except ImportError:
        return None
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "all",
        "noplaylist": True,
        "socket_timeout": YT_SOCKET_TIMEOUT,
        "retries": 1,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:  # noqa: BLE001
        return None

    fmts = info.get("formats") or []
    hls = [f for f in fmts if "m3u8" in (f.get("protocol") or "")]
    video: dict[int, dict] = {}
    for f in hls:
        h = f.get("height") or 0
        # предпочитаем h264 (avc1) — его точно декодирует CEF
        if not h or (f.get("vcodec") or "none") == "none":
            continue
        cur = video.get(h)
        if cur is None or (
            str(f.get("vcodec", "")).startswith("avc")
            and not str(cur.get("vcodec", "")).startswith("avc")
        ):
            video[h] = f
    audio = [f for f in hls if not f.get("height") and (f.get("vcodec") or "none") == "none"]
    if not video or not audio:
        return None

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Звук",'
        f"DEFAULT=YES,AUTOSELECT=YES,"
        f'URI="/hls?url={urllib.parse.quote(audio[0]["url"], safe="")}"',
    ]
    for h in sorted(video, reverse=True):
        f = video[h]
        br = int(f.get("tbr") or max(400, h * 4.5))
        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={br * 1000},RESOLUTION={int(h * 16 / 9)}x{h},AUDIO="aud"'
        )
        lines.append(f"/hls?url={urllib.parse.quote(f['url'], safe='')}")
    return "\n".join(lines) + "\n"


def build_master_playlist(url: str) -> str | None:
    """
    Собрать master-плейлист из отдельных HLS-потоков разных качеств.

    Rutube (и некоторые другие) отдают 360p/720p/1080p как ОТДЕЛЬНЫЕ m3u8,
    без master-плейлиста. hls.js видит один уровень и не может переключать
    качество. Мы строим master сами — тогда в плеере появляется выбор.
    """
    try:
        fmts = yt_formats(url)
    except Exception:  # noqa: BLE001
        return None

    # только HLS-форматы с разрешением
    variants: dict[int, dict] = {}
    for f in fmts:
        u = f.get("url") or ""
        if ".m3u8" not in u:
            continue
        h = f.get("height") or 0
        if not h:
            continue
        # оставляем по одному представителю на каждую высоту
        if h not in variants:
            variants[h] = f

    if len(variants) < 2:
        return None  # переключать нечего

    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for h in sorted(variants, reverse=True):  # от лучшего к худшему
        f = variants[h]
        br = f.get("tbr") or 0
        if not br:
            # оценка битрейта, если источник её не сообщил
            br = max(400, int(h * 4.5))
        width = int(h * 16 / 9) if h else 0
        lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={int(br * 1000)},RESOLUTION={width}x{h}")
        lines.append(f["url"])
    return "\n".join(lines) + "\n"


def remux_to_local(src: str, local_id: str) -> Path:
    """m3u8/DASH → локальный mp4 через ffmpeg -c copy (без перекодирования)."""
    from .infra.ffmpeg_bin import require_ffmpeg

    ffmpeg_exe = require_ffmpeg()
    out = cache.CACHE_DIR / f"{local_id}.mp4"
    proc = subprocess.Popen(
        [
            ffmpeg_exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-user_agent",
            USER_AGENT,
            "-i",
            src,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(out),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    _ffmpeg_procs.add(proc)
    try:
        _, err = proc.communicate(timeout=600)
    finally:
        _ffmpeg_procs.discard(proc)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size < 10_000:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg не смог собрать поток: {err.strip()[:300]}")
    return out


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "GTAVideoProxy/1.0"
    proxy_base = "http://127.0.0.1:9000"

    # ---------- утилиты ответа ----------
    # Шумные маршруты: HLS-сегменты и опрос состояния идут постоянно,
    # писать каждый в лог — значит спамить консоль сотнями строк в минуту.
    QUIET_PREFIXES = ("/hls", "/cmd/", "/report/", "/state/", "/static/")

    def log_message(self, fmt: str, *args) -> None:
        # args[0] — строка запроса вида '"GET /hls?url=... HTTP/1.1"'
        request_line = str(args[0]) if args else ""
        status = args[1] if len(args) > 1 else "?"
        quiet = any(p in request_line for p in self.QUIET_PREFIXES)
        # ВАЖНО: /hls раньше в лог не попадал ВООБЩЕ, поэтому ошибки сегментов
        # были невидимы. Теперь каждый плейлист/сегмент пишется в журнал
        # диагностики (с кодом ответа), а в консоль — только ошибки.
        if "/hls" in request_line or "/rutube-master" in request_line:
            try:
                part = request_line.split("GET ")[-1].split(" ")[0]
                target = urllib.parse.parse_qs(urllib.parse.urlsplit(part).query).get("url", [""])[
                    0
                ]
                name = diag.short_url(target) if target else part
            except Exception:  # noqa: BLE001
                name = request_line[:80]
            kind = "req" if str(status)[:1] in ("2", "3") else "err"
            diag.log(kind, f"{status} {name}", code=status)
            if kind == "err":
                try:
                    sys.stderr.write(
                        "[%s] ОШИБКА %s %s\n" % (self.log_date_time_string(), status, name)
                    )
                except (UnicodeEncodeError, OSError):
                    pass
            return
        if quiet:
            return  # остальные шумные маршруты не пишем
        try:
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))
        except (UnicodeEncodeError, OSError):
            pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_local_file(self, path: Path) -> None:
        """Отдать локальный файл с Range (та же логика, что в server.py)."""
        size = path.stat().st_size
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        range_h = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)", range_h) if range_h else None
        if m:
            start = int(m.group(1) or 0)
            end = int(m.group(2)) if m.group(2) else size - 1
            end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with path.open("rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with path.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                self.wfile.write(chunk)

    def _probe_codecs(self, local_path: Path) -> str:
        """Узнать кодеки файла через ffprobe — чтобы объяснить ошибку 4."""
        try:
            from .infra.ffmpeg_bin import find_ffmpeg

            ff = find_ffmpeg()
            if not ff:
                return ""
            probe = ff.replace("ffmpeg", "ffprobe")
            if not Path(probe).is_file():
                # imageio-ffmpeg не содержит ffprobe — берём ffmpeg -i
                proc = subprocess.run(
                    [ff, "-hide_banner", "-i", str(local_path)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                text = proc.stderr or ""
            else:
                proc = subprocess.run(
                    [
                        probe,
                        "-v",
                        "error",
                        "-show_entries",
                        "stream=codec_name,codec_type",
                        "-of",
                        "csv=p=0",
                        str(local_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                text = proc.stdout or ""
        except Exception:  # noqa: BLE001
            return ""

        text_low = text.lower()
        notes = []
        if "hevc" in text_low or "h265" in text_low:
            notes.append("видео в H.265/HEVC — CEF его не поддерживает")
        if "ac3" in text_low or "eac3" in text_low:
            notes.append("звук в AC3/E-AC3 — CEF его не поддерживает")
        if "dts" in text_low:
            notes.append("звук в DTS — CEF его не поддерживает")
        if "av1" in text_low:
            notes.append("видео в AV1 — старая сборка CEF может не поддерживать")
        if notes:
            return "; ".join(notes)
        if "h264" in text_low or "avc" in text_low:
            return "кодек H.264 — должен поддерживаться, файл мог скачаться не полностью"
        return ""

    def _diagnose_video_error(self, sid: str) -> str:
        """Объяснить ошибку 4 на основе того, что скачано в кеш."""
        # ищем последний файл сессии в кеше
        try:
            files = sorted(cache.CACHE_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
            if not files:
                return ""
            hint = self._probe_codecs(files[-1])
            return hint
        except Exception:  # noqa: BLE001
            return ""

    def _is_playlist_or_html(self, url: str, ctype: str) -> bool:
        """Это плейлист (HLS/DASH) или HTML-страница — <video> такое не играет."""
        low_url = url.lower()
        low_ct = (ctype or "").lower()
        if ".m3u8" in low_url or ".mpd" in low_url:
            return True
        playlist_types = (
            "application/vnd.apple.mpegurl",
            "application/x-mpegurl",
            "audio/mpegurl",
            "audio/x-mpegurl",
            "application/dash+xml",
        )
        if any(t in low_ct for t in playlist_types):
            return True
        return "text/html" in low_ct or "application/xhtml" in low_ct

    def _convert_to_file(self, original_url: str, media_url: str, ctype: str) -> None:
        """
        Превратить плейлист или страницу в настоящий MP4 через ffmpeg.
        Первый запрос ждёт конвертацию, дальше файл отдаётся мгновенно из кеша.
        """
        local_id = cache._build_filename(original_url)
        existing = cache.pathy_for(local_id)
        if existing:
            return self._serve_local_file(existing)

        if "text/html" in (ctype or "").lower():
            return self._send_text(
                409,
                "<h3>Ссылка ведёт на HTML-страницу, а не на видеофайл</h3>"
                "<p>Страницу вставить в телевизор нельзя. Нужна ссылка на сам поток "
                "или файл (.mp4 / .webm / .m3u8).</p>"
                "<p>Подсказка: откройте страницу в браузере, нажмите F12 → Network → "
                "фильтр Media, найдите .mp4 или .m3u8 и скопируйте её адрес.</p>",
            )

        # плейлист -> MP4 через ffmpeg (без перекодирования, быстро)
        try:
            local = remux_to_local(media_url, local_id)
        except Exception as e:  # noqa: BLE001
            return self._send_text(
                500,
                f"<h3>Не удалось собрать поток в MP4</h3><p>{e}</p>"
                "<p>Проверьте, что установлен ffmpeg (start.bat ставит его автоматически).</p>",
            )
        return self._serve_local_file(local)

    def _stream_remote(self, url: str) -> None:
        """Проксировать чужой файл байт-в-байт, сохраняя Range (URL остаётся валидным для CEF)."""
        range_header = self.headers.get("Range")
        try:
            self._stream_via_urllib(url, range_header)
        except Exception:
            # Cloudflare и др. режут Python-urllib по TLS-отпечатку, а curl проходит.
            # curl есть в Windows 10/11 и в большинстве дистрибутивов Linux.
            self._stream_via_curl(url, range_header)

    def _stream_via_urllib(self, url: str, range_header: str | None) -> None:
        req = urllib.request.Request(url, headers=self._headers_for(url))
        if range_header:
            req.add_header("Range", range_header)
        remote = urllib.request.urlopen(req, timeout=30)
        with remote:
            self.send_response(remote.status)
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                if remote.headers.get(h):
                    self.send_header(h, remote.headers[h])
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                while chunk := remote.read(64 * 1024):
                    self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # CEF закрыл соединение — нормально при перемотке

    def _stream_via_curl(self, url: str, range_header: str | None) -> None:
        """
        Обход антибот-челленджей: curl с браузерными заголовками.

        curl отдаёт заголовки и тело в один поток, разделённые пустой
        строкой. Разбор заголовков и пересылка тела — отдельные методы,
        чтобы каждый отвечал за одно.
        """
        curl = shutil.which("curl") or shutil.which("curl.exe")
        if not curl:
            self._send_text(502, "<h3>Источник недоступен</h3><p>curl не найден в системе</p>")
            return

        proc = subprocess.Popen(
            self._curl_command(curl, url, range_header),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            headers_raw = self._read_curl_headers(proc)
            if headers_raw is None:
                return
            status, out_headers = self._parse_curl_headers(headers_raw)
            self.send_response(status if status in (200, 206) else 502)
            for name in ("content-type", "content-length", "content-range", "accept-ranges"):
                if out_headers.get(name):
                    self.send_header(name.title(), out_headers[name])
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self._pump_curl_body(proc)
        finally:
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait(timeout=10)

    def _curl_command(self, curl: str, url: str, range_header: str | None) -> list[str]:
        """Собрать команду curl с браузерными заголовками."""
        cmd = [curl, "-sS", "-L", "--max-time", "120", "-A", USER_AGENT, "-D", "-", "-o", "-"]
        if range_header:
            cmd.extend(["-H", f"Range: {range_header}"])
        for name, value in BROWSER_HEADERS.items():
            if name != "User-Agent":
                cmd.extend(["-H", f"{name}: {value}"])
        cmd.append(url)
        return cmd

    def _read_curl_headers(self, proc: subprocess.Popen) -> bytes | None:
        """Прочитать блок заголовков до пустой строки. None — источник молчит."""
        headers_raw = b""
        while True:
            line = proc.stdout.readline() if proc.stdout else b""
            if not line:
                self._send_text(502, "<h3>Источник не отвечает через curl</h3>")
                proc.kill()
                return None
            if line.strip() in (b"", b"\r"):
                return headers_raw
            headers_raw += line

    @staticmethod
    def _parse_curl_headers(headers_raw: bytes) -> tuple[int, dict[str, str]]:
        """Разобрать «HTTP/1.1 200 OK\r\nK: V…» в (код, словарь заголовков)."""
        status = 200
        out_headers: dict[str, str] = {}
        for line in headers_raw.decode("iso-8859-1", "replace").splitlines():
            if line.upper().startswith("HTTP/"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    status = int(parts[1])
                continue
            if ":" in line:
                name, value = line.split(":", 1)
                out_headers[name.strip().lower()] = value.strip()
        return status, out_headers

    def _pump_curl_body(self, proc: subprocess.Popen) -> None:
        """Перекачать тело ответа клиенту порциями по 1 МБ."""
        if not proc.stdout:
            return
        try:
            while chunk := proc.stdout.read(1024 * 1024):
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # клиент закрыл соединение — это норма при перемотке

    # ---------- маршруты ----------
    def do_GET(self) -> None:  # noqa: N802
        """
        Диспетчер GET-запросов.

        Раньше это был один метод на 458 строк со сложностью 90 — найти в нём
        нужный маршрут было невозможно. Теперь здесь только разбор адреса и
        таблица «адрес → обработчик», а логика лежит в методах _route_*:
        один маршрут = один метод = одна ответственность.
        """
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        target = (qs.get("url") or [""])[0] or ""

        # Таблица отдаёт РОВНО ОДИН обработчик (или None, если адрес не наш).
        #
        # Раньше здесь был цикл, считавший None признаком «маршрут не подошёл».
        # Но обработчики возвращают None, УЖЕ отправив ответ (_send_text и др.),
        # поэтому каждый запрос получал второй ответ — 404 поверх нормального
        # (в журнале видно пары «200», «404» на один GET). Ответ уже отправлен,
        # повторный ломает соединение и путает плеер.
        handler = self._routes(path)
        if handler is None:
            return self._send_text(
                404,
                "<h3>404</h3><p>Маршруты: / , /yt?url= , /r?url= , /get?url= , "
                "/p/&lt;id&gt; , /resolve , /list</p>",
            )
        # Обработчик вернул False — значит, адрес подошёл по форме,
        # но не опознан внутри (например, /p/<id> с неизвестным id).
        if handler(path, qs, target) is False:
            return self._send_text(
                404,
                "<h3>404</h3><p>Маршруты: / , /yt?url= , /r?url= , /get?url= , "
                "/p/&lt;id&gt; , /resolve , /list</p>",
            )
        return None

    def _routes(self, path: str):
        """
        Таблица маршрутов в порядке проверки.

        Точные адреса проверяются раньше префиксов: /pin/status должен
        попасть в свой обработчик, а не в /pin.
        """
        exact = {
            "/": self._route_index,
            "/index.html": self._route_index,
            "/sessions": self._route_sessions,
            "/sessions/": self._route_sessions,
            "/subs": self._route_subs,
            "/pin": self._route_pin,
            "/pins": self._route_pins,
            "/pin/status": self._route_pin_status,
            "/pin/log": self._route_pin_log,
            "/unpin": self._route_unpin,
            "/diag": self._route_diagnostics,
            "/diag/clear": self._route_diagnostics_clear,
            "/direct": self._route_hls_bridge,
            "/hls": self._route_hls_bridge,
            "/yt-master": self._route_yt_master,
            "/kodik-master": self._route_kodik_master,
            "/rutube-master": self._route_rutube_master,
            "/twitch-master": self._route_twitch_master,
            "/new": self._route_new_session,
            "/resolve": self._route_api,
            "/list": self._route_api,
            "/yt": self._route_api,
            "/r": self._route_api,
            "/get": self._route_api,
        }
        if path in exact:
            return exact[path]

        prefixes = (
            ("/player/", self._route_player_page),
            ("/cmd/", self._route_commands),
            ("/report/", self._route_report),
            ("/state/", self._route_state),
            ("/control/", self._route_control),
            ("/static/", self._route_static),
            ("/trans/", self._route_translation),
            ("/local/", self._route_local_folder),
            ("/v/", self._route_local_file),
        )
        for prefix, handler in prefixes:
            if path.startswith(prefix):
                return handler

        return self._route_short_link

    def _route_index(self, path: str, qs: dict, target):
        """1) корень — оглавление"""
        return self._index()

    def _route_player_page(self, path: str, qs: dict, target):
        """1б) СТРАНИЦА-ПЛЕЕР: /player/<sid>"""
        sid = path[len("/player/") :].strip("/")
        sess = PLAYER_SESSIONS.get(sid)
        if not sess:
            return self._send_text(404, "<h3>Сессия не найдена</h3><p>Создайте новую в оверлее</p>")
        base = self._base()
        page_html = player_mod.render_player(sid, sess, base)
        body = page_html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return

    def _route_commands(self, path: str, qs: dict, target):
        """1в) опрос команд страницей: /cmd/<sid>?since=N"""
        sid = path[len("/cmd/") :].strip("/")
        since = safe_int((qs.get("since") or ["0"])[0], 0)
        seq, cmds = PLAYER_SESSIONS.poll_commands(sid, since)
        return self._send_json(200, {"seq": seq, "commands": cmds})

    def _route_report(self, path: str, qs: dict, target):
        """1г) состояние от страницы: /report/<sid>"""
        sid = path[len("/report/") :].strip("/")
        length = safe_int(self.headers.get("Content-Length"), 0)
        length = min(max(length, 0), 1024 * 1024)  # тело запроса состояния крошечное
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            payload = {}
        PLAYER_SESSIONS.set_state(sid, payload if isinstance(payload, dict) else {})
        # Ошибку из игры видно ТОЛЬКО здесь: пишем в журнал и в файл.
        err = (payload or {}).get("error") if isinstance(payload, dict) else ""
        if err:
            diag.log(
                "player",
                f"ошибка в игре: {err}",
                sid=sid,
                quality=(payload or {}).get("quality", ""),
            )
        return self._send_json(200, {"ok": True})

    def _route_sessions(self, path: str, qs: dict, target):
        """1г1) список активных сессий (оверлей показывает «что где играет»)"""
        return self._send_json(
            200,
            {
                "sessions": PLAYER_SESSIONS.summary(),
                "base": self._base(),
            },
        )

    def _route_state(self, path: str, qs: dict, target):
        """1г2) состояние для оверлея: /state/<sid>"""
        sid = path[len("/state/") :].strip("/")
        st = PLAYER_SESSIONS.get_state(sid)
        if not st:
            return self._send_json(404, {"error": "сессия не найдена"})
        # при ошибке видео — добавить человеческое объяснение
        if st.get("error") and "4" in str(st.get("error")):
            hint = self._diagnose_video_error(sid)
            if hint:
                st["errorHint"] = hint
        return self._send_json(200, st)

    def _route_control(self, path: str, qs: dict, target):
        """1д) управление из оверлея: /control/<sid>?cmd=play&value=0.5"""
        sid = path[len("/control/") :].strip("/")
        cmd = (qs.get("cmd") or [""])[0]
        raw_value = (qs.get("value") or [None])[0]
        value: object = raw_value
        if raw_value is not None:
            low = raw_value.lower()
            if low in ("true", "1", "on"):
                value = True
            elif low in ("false", "0", "off"):
                value = False
            else:
                try:
                    value = float(raw_value)
                except ValueError:
                    value = raw_value
        seq = PLAYER_SESSIONS.push_command(sid, cmd, value)
        if seq is None:
            return self._send_json(404, {"error": "сессия не найдена"})
        return self._send_json(200, {"ok": True, "seq": seq, "cmd": cmd, "value": value})

    def _route_subs(self, path: str, qs: dict, target):
        """1е) субтитры: /subs?url=..."""
        if not target:
            return self._send_text(400, "<h3>Нужен url</h3>")
        return self._serve_subtitles(target)

    def _route_static(self, path: str, qs: dict, target):
        """1и) статика для плеера (hls.js хранится локально)"""
        name = Path(path[len("/static/") :]).name  # только имя файла
        # hls.js лежит внутри пакета (web/assets) — так он работает и после
        # установки через pip, и при запуске из папки проекта.
        candidate = (BASE_DIR / "web" / "assets" / name).resolve()
        if candidate.is_file() and candidate.suffix in (".js", ".css", ".map"):
            return self._serve_static(candidate)
        return self._send_text(404, "<h3>Файл не найден</h3>")

    def _route_hls_bridge(self, path: str, qs: dict, target):
        """/direct, /hls — HLS-мост для hls.js"""
        if not target:
            return self._send_text(400, "<h3>Нужен ?url=</h3>")
        return self._serve_hls(target)

    def _route_pin(self, path: str, qs: dict, target):
        """1д1) ЗАКРЕПИТЬ ссылку: /pin?url=...[&referer=...&cookies=...]"""
        #   Превращает «живую только в сессии браузера» ссылку в постоянную:
        #   запоминает заголовки/cookie, качает файл локально и отдаёт /v/<id>.
        if path == "/pin":
            if not target:
                return self._send_json(400, {"ok": False, "error": "нужен ?url="})
            referer = (qs.get("referer") or [""])[0]
            cookies = (qs.get("cookies") or [""])[0]
            title = (qs.get("title") or [""])[0]
            # уже закреплённая запись? берём сохранённый контекст
            prev = pins.get(pins._pid_for(target))
            if prev and not referer:
                referer = prev.get("referer", "")
            rec = pins.add(target, referer=referer, cookies=cookies, title=title)
            pins.prepare(rec["id"])
            return self._send_json(
                200,
                {
                    "ok": True,
                    "id": rec["id"],
                    "state": "downloading",
                    "link": f"{self._base()}/v/{rec['id']}.mp4",
                    "direct": f"{self._base()}/v/{rec['id']}.mp4",
                },
            )

    def _route_pins(self, path: str, qs: dict, target):
        """/pins — список закреплённых ссылок"""
        rows = []
        for r in pins.all():
            rows.append(
                {
                    "id": r["id"],
                    "url": r["url"],
                    "title": r.get("title", ""),
                    "state": r.get("state", ""),
                    "progress": r.get("progress", 0),
                    "size": r.get("size", 0),
                    "error": r.get("error", ""),
                    "created": r.get("created", 0),
                    "link": f"{self._base()}/v/{r['id']}.mp4",
                }
            )
        return self._send_json(200, {"pins": rows, "count": len(rows)})

    def _route_pin_status(self, path: str, qs: dict, target):
        """/pin/status?id= — состояние одной закреплённой ссылки"""
        pid = (qs.get("id") or [""])[0]
        rec = pins.get(pid)
        if not rec:
            return self._send_json(404, {"ok": False, "error": "не найдено"})
        rec["link"] = f"{self._base()}/v/{rec['id']}.mp4"
        return self._send_json(200, rec)

    def _route_pin_log(self, path: str, qs: dict, target):
        """/pin/log?id= — журнал закрепления"""
        pid = (qs.get("id") or [""])[0]
        return self._send_json(200, {"id": pid, "log": pins.log_for(pid)})

    def _route_unpin(self, path: str, qs: dict, target):
        """/unpin?id= — удалить закрепление"""
        pid = (qs.get("id") or [""])[0]
        return self._send_json(200, {"ok": pins.remove(pid)})

    def _route_local_file(self, path: str, qs: dict, target):
        """1д3) постоянная ссылка: /v/<id>.mp4 — отдаёт локальный файл"""
        pid = urllib.parse.unquote(path[len("/v/") :]).strip("/")
        if pins.serve_partial(self, pid):
            return True  # файл уже отдан (200/206/416)
        rec = pins.get(pid.split(".")[0])
        if rec and rec.get("state") == "downloading":
            pins.prepare(rec["id"])
            return self._send_text(
                409,
                "<h3>Файл ещё готовится</h3>"
                "<p>Ссылка уже постоянная — обновите страницу через минуту.</p>",
            )
        return self._send_text(
            404,
            "<h3>Ссылка не найдена</h3><p>Закрепите поток в оверлее кнопкой «Закрепить».</p>",
        )

    def _route_diagnostics(self, path: str, qs: dict, target):
        """/diag — журнал событий + снимок окружения"""
        return self._send_json(
            200,
            {
                "snapshot": diag.snapshot(),
                "events": diag.recent(safe_int((qs.get("limit") or ["300"])[0], 300)),
                "report": diag.report_text(),
            },
        )

    def _route_diagnostics_clear(self, path: str, qs: dict, target):
        """/diag/clear — очистить журнал"""
        diag.clear()
        return self._send_json(200, {"ok": True})

    def _route_yt_master(self, path: str, qs: dict, target):
        """1е1) YouTube: master из видео- и аудио-дорожек HLS"""
        if not target:
            return self._send_text(400, "<h3>Нужен ?url=</h3>")
        master = build_yt_master(target)
        if not master:
            return self._send_text(502, "<h3>YouTube не отдал HLS</h3>")
        body = master.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return

    def _route_kodik_master(self, path: str, qs: dict, target):
        """1е2) Kodik: master-плейлист из отдельных потоков"""
        if not target:
            return self._send_text(400, "<h3>Нужен ?url=</h3>")
        tr = (qs.get("trans") or [None])[0]
        try:
            tr_id = int(tr) if tr not in (None, "", "None") else None
        except ValueError:
            tr_id = None
        ref = (qs.get("ref") or [""])[0]
        info = sources.kodik_master(target, tr_id, referer=ref)
        if not info:
            return self._send_text(502, "<h3>Kodik не отдал поток</h3>")
        body = info["playlist"].encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return

    def _route_rutube_master(self, path: str, qs: dict, target):
        """1е2а) Rutube: master со всеми качествами (360p/720p/1080p)."""
        #   hls.js показывает список в «Качество» и переключается адаптивно —
        #   если канал не тянет 1080p, плеер сам уйдёт на 720p, а не упадёт.
        if path == "/rutube-master":
            if not target:
                return self._send_text(400, "<h3>Нужен ?url=</h3>")
            try:
                info = sources.rutube_stream(target)
            except Exception as e:  # noqa: BLE001
                return self._send_text(502, f"<h3>Rutube: {e}</h3>")
            if not info or not info.get("quality_files"):
                return self._send_text(502, "<h3>Rutube не отдал качества</h3>")
            lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
            base = info.get("master_base") or ""
            files = info["quality_files"]
            # от лучшего к худшему
            for h in sorted((int(k) for k in files), reverse=True):
                url_h = base + files[str(h)]
                bw = h * 8000  # оценка битрейта: 1080p ≈ 8.6 Мбит/с
                lines.append(
                    f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={int(h * 16 / 9)}x{h},NAME="{h}p"'
                )
                lines.append(f"/hls?url={urllib.parse.quote(url_h, safe='')}")
            body = "\n".join(lines).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return True  # плейлист уже отправлен

    def _route_twitch_master(self, path: str, qs: dict, target):
        """1е2б) Twitch: master-плейлист (стрим, VOD, клип)"""
        if not target:
            return self._send_text(400, "<h3>Нужен ?url=</h3>")
        try:
            info = sources.twitch_playlist(target)
        except Exception as e:  # noqa: BLE001
            return self._send_text(502, f"<h3>Twitch: {e}</h3>")
        if not info:
            return self._send_text(502, "<h3>Twitch не отдал поток</h3>")
        body = info["playlist"].encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return

    def _route_translation(self, path: str, qs: dict, target):
        """1е3) Смена озвучки: /trans/<sid>?index=N&pos=12"""
        sid = path[len("/trans/") :].strip("/")
        sess = PLAYER_SESSIONS.get(sid)
        if not sess:
            return self._send_json(404, {"ok": False, "error": "сессия не найдена"})
        try:
            idx = int((qs.get("index") or ["0"])[0])
        except ValueError:
            idx = 0
        if not (0 <= idx < len(sess.translations)):
            return self._send_json(400, {"ok": False, "error": "нет такой озвучки"})
        PLAYER_SESSIONS.set_translation_index(sid, idx)
        item = sess.translations[idx]
        url = item.get("url") or ""
        # у озвучек animego свой плеер Kodik — пересобираем master под него
        if not url and item.get("player"):
            url = self._kodik_master_url(item["player"], None, sess.referer)
        return self._send_json(
            200,
            {
                "ok": True,
                "index": idx,
                "title": item.get("title", ""),
                "src": url,
            },
        )

    def _route_local_folder(self, path: str, qs: dict, target):
        """2) локальные файлы из папки videos/"""
        name = urllib.parse.unquote(path[len("/local/") :])
        candidate = (Path("./videos") / name).resolve()
        try:
            candidate.relative_to(Path("./videos").resolve())
        except ValueError:
            return self._send_text(403, "<h3>Запрещено</h3>")
        video_suffixes = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".mp3", ".ogg", ".wav"}
        if candidate.is_file() and candidate.suffix.lower() in video_suffixes:
            return self._serve_local_file(candidate)
        return self._send_text(404, "<h3>Файл не найден в videos/</h3>")

    def _route_short_link(self, path: str, qs: dict, target):
        """3) короткие ссылки /p/<id>"""
        m = PLAY_ID_RE.match(path)
        if m:
            url = REGISTRY.get(m.group(1))
            if not url:
                return self._send_text(404, "<h3>Ссылка не найдена</h3>")
            return self._handle_resolve(url, short=True)
        return False  # это не /p/<id> — адрес не наш

    def _route_api(self, path: str, qs: dict, target):
        """API: /resolve, /list, /yt, /get, /r — разные действия по адресу."""
        handlers = {
            "/resolve": self._api_resolve,
            "/list": self._api_list,
            "/yt": self._api_yt,
            "/get": self._api_get,
            "/r": self._api_r,
        }
        handler = handlers.get(path)
        if handler is None:
            return self._send_text(
                404,
                "<h3>404</h3><p>Маршруты: / , /yt?url= , /r?url= , /get?url= , "
                "/p/&lt;id&gt; , /resolve , /list</p>",
            )
        return handler(target)

    def _api_resolve(self, target: str):
        """/resolve?url= — разобрать ссылку и вернуть JSON о источнике."""
        if not target:
            return self._send_json(400, {"error": "нужен параметр url"})
        try:
            info = resolve_input(target)
        except Exception as e:  # noqa: BLE001 — любая ошибка разбора → 500
            return self._send_json(500, {"error": str(e)})
        short = register_url(target)
        info["short_url"] = f"{self.proxy_base}/p/{short}"
        info["tested"] = bool(info.get("url")) and test_url(info["url"])
        return self._send_json(200, info)

    def _api_list(self, target: str):
        """/list — все зарегистрированные короткие ссылки."""
        rows = [
            {"id": rid, "url": url, "short": f"{self.proxy_base}/p/{rid}"}
            for rid, url in REGISTRY.items()
        ]
        return self._send_json(200, {"items": rows})

    def _api_yt(self, target: str):
        """/yt?url= — YouTube-страница с прямым потоком."""
        if not target:
            return self._send_text(400, "<h3>Нужен параметр ?url=</h3>")
        return self._handle_yt(target)

    def _api_get(self, target: str):
        """/get?url= — проксировать файл, HTML/плейлист сконвертировать."""
        if not target:
            return self._send_text(400, "<h3>Нужен ?url=</h3>")
        # Проверяем реальный тип ДО проксирования: HTML/плейлист
        # в <video> дадут код 4.
        ctype, _ = probe_content_type(target)
        if self._is_playlist_or_html(target, ctype or ""):
            return self._convert_to_file(target, target, ctype or "")
        return self._stream_remote(target)

    def _api_r(self, target: str):
        """/r?url= — страница-плеер для произвольного источника."""
        if not target:
            return self._send_text(400, "<h3>Нужен параметр ?url=</h3>")
        return self._handle_resolve(target)

    def do_HEAD(self) -> None:  # noqa: N802
        """CEF часто делает HEAD перед воспроизведением — отвечаем как GET без тела."""
        self.do_GET()

    def _route_new_session(self, path: str, qs: dict, target):
        """
        /new?url=...&mode=... — создать сессию и подготовить источник.

        Состоит из четырёх явных шагов (см. методы ниже): найти настоящий
        плеер на странице, проверить ссылку, создать сессию, подготовить
        поток. Раньше всё это лежало в одном блоке на 81 строку.
        """
        if not target:
            return self._send_text(400, "<h3>Нужен ?url=</h3>")

        started = time.time()
        mode = (qs.get("mode") or ["r"])[0]
        title = (qs.get("title") or [""])[0]
        sub = (qs.get("sub") or [""])[0]
        with_check = (qs.get("check") or ["1"])[0] != "0"

        target, referer = self._find_real_player(target, mode)

        warning = ""
        if with_check:
            ok, problem = self._quick_check(target, mode)
            if not ok:
                return self._send_json(
                    200,
                    {
                        "ok": False,
                        "reason": problem,
                        "hint": "Проверьте ссылку в браузере: если видео там играет, "
                        "попробуйте режим «Прямой файл» — ссылка на .mp4/.webm",
                    },
                )
            warning = problem

        sess = PLAYER_SESSIONS.create(url=target, mode=mode, title=title, sub=sub)
        if referer:
            sess.referer = referer

        extra = self._prepare_source(sess)
        elapsed = round(time.time() - started, 2)
        if extra.get("error"):
            PLAYER_SESSIONS.delete(sess.sid)
            diag.log(
                "err",
                f"источник не подготовлен за {elapsed:.1f} с: {extra['error']}",
                sec=elapsed,
                url=diag.short_url(sess.url),
            )
            return self._send_json(
                200,
                {
                    "ok": False,
                    "reason": extra["error"],
                    "hint": extra.get("hint", ""),
                },
            )

        return self._session_ready_response(sess, extra, warning, elapsed)

    def _find_real_player(self, target: str, mode: str) -> tuple[str, str]:
        """
        Найти настоящий плеер внутри страницы кинотеатра.

        Возвращает (ссылка_на_плеер, referer). Если страница — уже плеер
        (Kodik, VK) или каталог аниме, возвращает ссылку без изменений:
        у каждой озвучки там свой медиа-хеш, и подмена на один плеер
        оставила бы только одну озвучку.
        """
        skip = sources.is_kodik(target) or sources.is_vk(target) or sources.is_anime_page(target)
        if skip or mode != "r" or not self._looks_like_site_page(target):
            return target, ""

        try:
            found = sources.find_player_iframes(target)
        except Exception:  # noqa: BLE001 — сайт может не отдать страницу
            found = []
        if not found:
            return target, ""

        # Kodik подписывает запросы под домен партнёра — запоминаем его.
        parts = urllib.parse.urlsplit(target)
        return found[0]["url"], f"{parts.scheme}://{parts.netloc}/"

    def _session_ready_response(self, sess, extra: dict, warning: str, elapsed: float) -> None:
        """Отметить сессию готовой, записать замер и ответить оверлею."""
        PLAYER_SESSIONS.set_status(sess.sid, "ready")
        diag.log(
            "new",
            f"готово за {elapsed:.1f} с: {diag.short_url(sess.url)}",
            sec=elapsed,
            quality=(extra.get("quality") or ""),
        )
        return self._send_json(
            200,
            {
                "ok": True,
                "sid": sess.sid,
                "player": f"{self._base()}/player/{sess.sid}",
                "control": f"{self._base()}/control/{sess.sid}",
                "warning": extra.get("warning", warning),
                "translations": [t.get("title") for t in sess.translations],
                "translation": sess.translation,
                "source": sess.source,
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        """POST нужен странице-плееру для отправки состояния."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/report/"):
            sid = path[len("/report/") :].strip("/")
            length = safe_int(self.headers.get("Content-Length"), 0)
            length = min(max(length, 0), 1024 * 1024)  # тело запроса состояния крошечное
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except (json.JSONDecodeError, ValueError):
                payload = {}
            PLAYER_SESSIONS.set_state(sid, payload if isinstance(payload, dict) else {})
            # Ошибка из игры доходит СЮДА (do_POST), и раньше она терялась:
            # в логе был только факт «страница что-то прислала».
            # «страница открылась» пишем только для первого, чистого контакта:
            # отчёт «через 12 с не заиграло» тоже помечен boot=1, но это не открытие.
            if isinstance(payload, dict) and payload.get("boot") and not payload.get("error"):
                # Первый контакт: видно, ЧТО за браузер в телевизоре и
                # загрузился ли hls.js. Без этого гадать бессмысленно.
                diag.log(
                    "boot",
                    "страница открылась в игре",
                    sid=sid,
                    ua=payload.get("ua", ""),
                    hls=payload.get("hlsjs", ""),
                    mse=payload.get("mse", ""),
                    native=payload.get("nativeHls", ""),
                    screen=payload.get("screen", ""),
                )
            if isinstance(payload, dict) and payload.get("error"):
                diag.log(
                    "player",
                    f"ошибка в игре: {payload['error']}",
                    sid=sid,
                    quality=payload.get("quality", ""),
                    ready=payload.get("readyState", ""),
                    level=payload.get("currentLevel", ""),
                )
            return self._send_json(200, {"ok": True})
        return self._send_json(404, {"error": "неизвестный POST-маршрут"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
        self.end_headers()

    # ---------- бизнес-логика ----------
    def _quick_check(self, url: str, mode: str) -> tuple[bool, str]:
        """
        Быстро проверить ссылку и вернуть (можно_ли_играть, пояснение).
        Никогда не висит дольше RESOLVE_TIMEOUT секунд.
        """
        # 1) заведомо закрытые сервисы
        walled, reason = is_login_walled(url)
        if walled:
            return False, reason

        # 2) режим "прямой файл" — проверяем, что это действительно файл
        if mode == "get":
            ctype, _ = probe_content_type(url)
            if ctype and ctype.startswith(("video/", "audio/")):
                return True, ""
            # HTML вместо видео — это гарантированный код 4 у <video>
            if ctype and ("text/html" in ctype or "xhtml" in ctype):
                return False, (
                    "Ссылка ведёт на HTML-страницу, а не на видеофайл — "
                    "телевизор не сможет её воспроизвести. Нужна прямая ссылка "
                    "на .mp4 / .webm / .m3u8"
                )
            if test_url(url):
                return True, ""
            # не смогли подтвердить — не блокируем, но предупреждаем
            return True, (
                "не удалось подтвердить, что это видеофайл "
                "(сервер мог скрыть тип) — пробуем всё равно"
            )

        # 3) YouTube — доверяем yt-dlp (у него свои таймауты)
        low = url.lower()
        if "youtube.com" in low or "youtu.be" in low:
            return True, ""

        # 4) прочие сайты — быстрый HEAD, без долгого разбора
        ctype, _size = probe_content_type(url)
        if ctype and ctype.startswith(("video/", "audio/")):
            return True, ""
        if ctype and "text/html" in ctype:
            return True, ""  # страница — пусть решает страница-плеер
        return True, ""

    def _base(self) -> str:
        """Базовый URL этого сервера (для ссылок внутри страницы-плеера)."""
        port = getattr(self.server, "server_port", 9000)
        host = self.headers.get("Host") or f"127.0.0.1:{port}"
        return f"http://{host}"

    # Расширения, которые уже ЯВЛЯЮТСЯ медиа: их нельзя отдавать в yt-dlp.
    # Раньше список был только m3u8/mp4/webm, и КАЖДЫЙ HLS-сегмент (.ts/.m4s)
    # уходил в yt-dlp — он падал с ошибкой на каждом запросе, сегменты не
    # успевали скачаться, и hls.js показывал manifestLoadError.
    MEDIA_EXTS = (
        ".m3u8",
        ".mp4",
        ".webm",
        ".ts",
        ".m4s",
        ".aac",
        ".m4a",
        ".mp3",
        ".vtt",
        ".mov",
        ".mkv",
        ".mpd",
    )
    # Ключи шифрования AES-128 HLS часто без расширения: /key, /key1, /enc.key
    MEDIA_HINTS = ("/key", "/keys/", "key.bin", "/init.mp4", "/init.m4s")

    def _resolve_media_url(self, url: str) -> str:
        """Страница сайта -> настоящий медиапоток (m3u8/mp4)."""
        low = url.lower().split("?")[0]
        if any(low.endswith(ext) for ext in self.MEDIA_EXTS):
            return url
        name = low.rsplit("/", 1)[-1]
        # имя файла без точки («key») — это точно не страница, отдаём как есть
        if name and "." not in name:
            return url
        if any(h in low for h in self.MEDIA_HINTS):
            return url
        try:
            info = resolve_input(url)
            if info.get("kind") in ("hls", "direct") and info.get("url"):
                return info["url"]
            fmts = info.get("formats") or []
            if fmts:
                # Сначала HLS — у VK/YouTube прогрессивные ссылки подписаны
                # на IP и в браузере дают 502.
                for f in fmts:
                    if ".m3u8" in (f.get("url") or ""):
                        return f["url"]
                best = pick_direct(fmts)
                if best:
                    return best
        except Exception:  # noqa: BLE001
            pass
        return url

    # ------------------------------------------------------------------
    #            ПОДГОТОВКА ИСТОЧНИКА (VK, Kodik, YouTube-HLS)
    # ------------------------------------------------------------------
    def _prepare_source(self, sess) -> dict:
        """
        Разобрать ссылку и решить, как её играть.

        Раньше это была одна функция со сложностью 28 и восемью вложенными
        ветками — по ней нельзя было понять, какой источник где обрабатывается.
        Теперь каждый источник — отдельный метод, а здесь только выбор:
        первый подходящий обработчик выигрывает. Порядок важен, поэтому он
        задан списком, а не порядком строк в теле функции.

        Возвращает {"error": ...} при понятной ошибке — вместо пустого
        экрана в игре. Заполняет sess.source / sess.translations.
        """
        for handler in (
            self._prepare_anime,
            self._prepare_kodik,
            self._prepare_twitch,
            self._prepare_rutube,
            self._prepare_generic_page,
            self._prepare_vk,
            self._prepare_youtube,
        ):
            result = handler(sess)
            if result is not None:
                return result
        return {}

    def _prepare_anime(self, sess) -> dict | None:
        """Страница аниме-каталога: у каждой озвучки свой плеер Kodik."""
        url = sess.url or ""
        if not (sources.is_anime_page(url) and not sources.is_kodik(url)):
            return None

        try:
            links = sources.animego_kodik_links(url)
        except Exception:  # noqa: BLE001 — страница может не отдать список
            links = []
        if not links:
            return None

        parts = urllib.parse.urlsplit(url)
        site_ref = f"{parts.scheme}://{parts.netloc}/"
        items = [
            {
                "title": link["title"] or "Озвучка",
                "url": self._kodik_master_url(link["url"], None, site_ref),
                "id": 0,
                "player": link["url"],
            }
            for link in links
        ]
        sess.source = "hls"
        sess.referer = site_ref
        sess.source_url = items[0]["url"]
        sess.translations = items
        sess.translation_index = 0
        sess.translation = items[0]["title"]
        PLAYER_SESSIONS.set_source(sess.sid, "hls")
        PLAYER_SESSIONS.set_translations(sess.sid, items, 0)
        return {"warning": "", "translation": sess.translation}

    def _prepare_kodik(self, sess) -> dict | None:
        """Kodik: несколько озвучек, поток через /ftor."""
        url = sess.url or ""
        if not sources.is_kodik(url):
            return None

        # Kodik подписывает запросы под домен партнёра. Если плеер открыт
        # с сайта кинотеатра, referer уже сохранён в сессии; иначе подберём.
        referer = sess.referer or sources.kodik_guess_referer(url)
        try:
            info = sources.kodik_streams(url, referer=referer)
        except Exception as e:  # noqa: BLE001 — Kodik может не ответить
            return {"error": f"Kodik не ответил: {e}"}
        if not info:
            return {
                "error": "Kodik не отдал ссылки на поток",
                "hint": "Возможно, серия недоступна или плеер сменил адрес. "
                "Попробуйте другую озвучку или другой плеер на сайте.",
            }

        sess.source = "hls"
        items, current = self._kodik_translation_items(url, info, referer)
        sess.source_url = items[current]["url"]
        sess.translations = items
        sess.translation_index = current
        sess.translation = items[current]["title"]
        sess.referer = info.get("referer") or referer
        PLAYER_SESSIONS.set_source(sess.sid, "hls")
        PLAYER_SESSIONS.set_translations(sess.sid, items, current)
        return {"warning": "", "translation": sess.translation}

    def _kodik_translation_items(
        self, url: str, info: dict, referer: str
    ) -> tuple[list[dict], int]:
        """Список озвучек Kodik и индекс текущей."""
        try:
            variants = sources.kodik_translations(url)
        except Exception:  # noqa: BLE001 — без списка останется одна озвучка
            variants = []
        if not variants:
            variants = [
                {
                    "id": info.get("translation_id", 0),
                    "title": info["translation"] or "Озвучка",
                }
            ]

        items: list[dict] = []
        current = 0
        for index, translation in enumerate(variants):
            items.append(
                {
                    "title": translation["title"],
                    "url": self._kodik_master_url(url, translation.get("id"), referer),
                    "id": translation.get("id", 0),
                }
            )
            if translation.get("id") == info.get("translation_id"):
                current = index
        return items, current

    def _prepare_twitch(self, sess) -> dict | None:
        """Twitch: стрим, VOD или клип."""
        url = sess.url or ""
        if not sources.is_twitch(url):
            return None

        try:
            info = sources.twitch_playlist(url)
        except Exception as e:  # noqa: BLE001 — Twitch может не ответить
            return {
                "error": f"Twitch не ответил: {e}",
                "hint": "Если это канал — он мог закончить эфир. Проверьте ссылку: "
                "twitch.tv/<канал>, twitch.tv/videos/<номер> или клип.",
            }
        if not info:
            return {
                "error": "Twitch не отдал поток",
                "hint": "Канал не в эфире, видео удалено или закрыто подпиской.",
            }

        sess.source = "hls"
        sess.source_url = f"{self._base()}/twitch-master?url={urllib.parse.quote(url, safe='')}"
        PLAYER_SESSIONS.set_source(sess.sid, "hls")
        note = "прямой эфир" if info.get("live") else "запись"
        return {"warning": "", "title": f"{info.get('title', '')} · {note}"}

    def _prepare_rutube(self, sess) -> dict | None:
        """
        Rutube: видео и звук одной HLS-дорожкой.

        Без этой ветки Rutube попадал в «любой сайт», где перехват находил
        только видео без звука (0p без аудио).
        """
        url = sess.url or ""
        if not _is_rutube(url):
            return None

        try:
            info = sources.rutube_stream(url)
        except Exception:  # noqa: BLE001 — падаем в общий разбор ниже
            return None
        if not info:
            return None

        sess.source = "hls"
        # Master со ВСЕМИ качествами: hls.js сам подберёт по скорости.
        # Одна дорожка 1080p (6.8 Мбит/с) не успевает на слабом канале
        # и даёт manifestLoadError — поэтому отдаём выбор плееру.
        if info.get("quality_files"):
            sess.source_url = f"{self._base()}/rutube-master?url={urllib.parse.quote(url, safe='')}"
        else:
            sess.source_url = info["url"]
        PLAYER_SESSIONS.set_source(sess.sid, "hls")
        return {"warning": "", "title": info.get("title", "")}

    def _prepare_generic_page(self, sess) -> dict | None:
        """
        Любой другой сайт: ищем поток прямо на странице.

        Это то же, что вкладка «Сеть → Медиа» в браузере: смотрим HTML,
        скрипты и iframe и вытаскиваем ссылки на .m3u8. Известные сайты
        (Rutube, YouTube, VK, Kodik, Twitch, animego) разбираются своими
        путями — их не перехватываем, иначе получаем неполный поток
        (например, только одну дорожку Rutube).
        """
        url = sess.url or ""
        known = (
            sources.is_kodik(url)
            or sources.is_vk(url)
            or sources.is_twitch(url)
            or sources.is_anime_page(url)
            or _is_rutube(url)
            or _is_known_media_host(url)
        )
        if sess.mode != "r" or not self._looks_like_site_page(url) or known:
            return None

        try:
            streams = sources.scrape_streams(url)
        except Exception:  # noqa: BLE001 — страница может не отдать поток
            streams = []
        if not streams:
            return None

        streams.sort(
            key=lambda item: (item.get("kind") == "hls", item.get("quality") or 0),
            reverse=True,
        )
        pick = streams[0]
        sess.source = "hls" if pick.get("kind") == "hls" else "file"
        sess.source_url = pick["url"]
        PLAYER_SESSIONS.set_source(sess.sid, sess.source)
        warning = f"нашлось потоков: {len(streams)}" if len(streams) > 1 else ""
        return {"warning": warning, "stream": pick["url"], "found": len(streams)}

    def _prepare_vk(self, sess) -> dict | None:
        """VK Видео: собственный master (видео + аудио)."""
        url = sess.url or ""
        if not sources.is_vk(url):
            return None

        try:
            master = sources.vk_master(url)
        except Exception as e:  # noqa: BLE001 — VK может не ответить
            return {"error": f"VK не ответил: {e}"}
        if not master:
            return {
                "error": "VK не отдал HLS-поток",
                "hint": "Возможно, видео требует входа в аккаунт или удалено. "
                "Попробуйте открыть ссылку в браузере.",
            }

        sess.source = "hls"
        sess.source_url = f"{self._base()}/hls?url={urllib.parse.quote(url, safe='')}"
        PLAYER_SESSIONS.set_source(sess.sid, "hls")
        return {"warning": ""}

    def _prepare_youtube(self, sess) -> dict | None:
        """YouTube: HLS напрямую (видео и звук отдельными дорожками)."""
        if sess.mode != "yt":
            return None
        url = sess.url or ""
        sess.source = "hls"
        sess.source_url = f"{self._base()}/yt-master?url={urllib.parse.quote(url, safe='')}"
        PLAYER_SESSIONS.set_source(sess.sid, "hls")
        return {"warning": ""}

    @staticmethod
    def _looks_like_site_page(url: str) -> bool:
        """Ссылка на HTML-страницу (а не на файл/поток)."""
        media_extensions = (".mp4", ".webm", ".m3u8", ".mpd", ".m4v", ".mov", ".ts")
        path = urllib.parse.urlparse(url).path.lower()
        return not path.endswith(media_extensions)

    def _kodik_master_url(
        self, player_url: str, trans_id: int | None = None, referer: str = ""
    ) -> str:
        """Ссылка на наш /kodik-master для плеера Kodik (с выбранной озвучкой)."""
        link = f"{self._base()}/kodik-master?url={urllib.parse.quote(player_url, safe='')}"
        if trans_id:
            link += f"&trans={trans_id}"
        if referer:
            link += f"&ref={urllib.parse.quote(referer, safe='')}"
        return link

    def _send_playlist(self, text: str) -> None:
        """Отдать готовый плейлист как есть (ссылки в нём уже через /hls)."""
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    @staticmethod
    def _headers_for(url: str) -> dict:
        """Заголовки под конкретный CDN: VK/YouTube хотят Chrome, остальные Firefox."""
        low = (url or "").lower()
        if any(d in low for d in _NEEDS_CHROME):
            return dict(CHROME_HEADERS)
        return dict(BROWSER_HEADERS)

    def _probe_playlist(self, url: str) -> tuple[str, str] | None:
        """
        Прочитать ответ и понять: плейлист это или кусок видео.
        Возвращает (текст_плейлиста, final_url) либо None, если это не плейлист.
        Тело сегмента не теряется: отдаём его сразу отсюда.
        """
        try:
            req = urllib.request.Request(url, headers=self._headers_for(url))
            remote = urllib.request.urlopen(req, timeout=25)
        except Exception as e:  # noqa: BLE001
            # Это и есть настоящая причина manifestLoadError в 90% случаев:
            # CDN не отдал плейлист. Пишем код и текст, а не глотаем.
            code = getattr(e, "code", "")
            reason = getattr(e, "reason", "")
            diag.log(
                "err",
                f"CDN не отдал плейлист: {diag.short_url(url)}",
                code=code,
                error=f"{type(e).__name__}: {reason or e}"[:200],
            )
            raise RuntimeError(str(e))

        with remote:
            head = remote.read(1024)
            if not head.startswith(b"#EXTM3U"):
                # обычный файл/сегмент: отдаём заголовки и тело сразу
                self.send_response(remote.status)
                ctype = remote.headers.get("Content-Type") or "application/octet-stream"
                self.send_header("Content-Type", ctype)
                length = remote.headers.get("Content-Length")
                if length:
                    self.send_header("Content-Length", length)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if self.command != "HEAD":
                    try:
                        self.wfile.write(head)
                        # ВАЖНО: порции по 64 КБ, а не по 1 МБ. Сегменты HLS
                        # небольшие, а мегабайтная буферизация добавляла
                        # несколько секунд на каждый и рвала живой поток.
                        while chunk := remote.read(64 * 1024):
                            self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    except OSError as e:
                        # обрыв при чтении с CDN — типичная причина «застряло»
                        diag.log(
                            "err",
                            f"обрыв при отдаче сегмента: {diag.short_url(url)}",
                            error=f"{type(e).__name__}: {e}"[:200],
                        )
                return None

            rest = remote.read()
            text = (head + rest).decode("utf-8", "replace")
            return text, remote.geturl()

    def _serve_hls(self, target: str) -> None:
        """
        Прокси для HLS: hls.js запрашивает плейлист и сегменты у нас,
        мы ходим к источнику. Обходит CORS и «протухание» ссылок.

        Шаги: (1) получить плейлист от источника, (2) переписать все ссылки
        внутри на наш прокси, (3) отдать. Каждый шаг — отдельный метод.
        """
        playlist = self._hls_source_playlist(target)
        if playlist is None:
            return  # это был сегмент: байты уже отданы клиенту
        raw, final_url = playlist
        self._send_playlist(self._rewrite_playlist(raw, final_url))

    def _hls_source_playlist(self, target: str) -> tuple[str, str] | None:
        """
        Получить исходный плейлист (текст, итоговый URL).

        VK и Kodik разбираем своей логикой и ДО тяжёлого резолва: yt-dlp
        на этих сайтах работает медленно, а ссылки внутри плейлистов
        относительные — их надо переписать на наш прокси.

        Возвращает None, если запрос оказался сегментом (он уже отдан).
        """
        if sources.is_vk(target):
            return self._hls_playlist_vk(target)
        if sources.is_kodik(target):
            return self._hls_playlist_kodik(target)
        return self._hls_playlist_resolved(target)

    def _hls_playlist_vk(self, target: str) -> tuple[str, str] | None:
        """Плейлист VK: сначала прямой CDN (если отдаёт #EXTM3U), потом master."""
        low = target.lower()
        is_segment = low.endswith((".ts", ".mp4", ".m4s", ".aac", ".m4a", ".vtt"))
        if "okcdn.ru" in low and not is_segment:
            try:
                req = urllib.request.Request(target, headers=self._headers_for(target))
                with urllib.request.urlopen(req, timeout=25) as response:
                    text = response.read().decode("utf-8", "replace")
                    if text.lstrip().startswith("#EXTM3U"):
                        return text, response.geturl()
            except Exception as e:  # noqa: BLE001 — CDN может отказать
                self._send_text(502, f"<h3>VK не отдал поток</h3><p>{e}</p>")
                return None

        master = sources.vk_master(target)
        if not master:
            return None
        base = master["audio"] or next(
            (
                line.split("url=")[1]
                for line in master["playlist"].splitlines()
                if line.startswith("/hls?url=")
            ),
            "",
        )
        base_url = urllib.parse.unquote(base.split("/hls?url=")[-1])
        return master["playlist"], base_url

    def _hls_playlist_kodik(self, target: str) -> tuple[str, str] | None:
        """Плейлист Kodik, собранный из отдельных потоков качеств."""
        master = sources.kodik_master(target)
        if not master:
            return None
        inner = sources.kodik_streams(target)
        referer = ""
        if inner and inner.get("qualities"):
            referer = next(iter(inner["qualities"].values()))
        return master["playlist"], referer

    def _hls_playlist_resolved(self, target: str) -> tuple[str, str] | None:
        """Плейлист через yt-dlp (или отдача сегмента, если это кусок видео)."""
        media_url = self._resolve_media_url(target)

        # Плейлист это или кусок видео? По URL не понять: у YouTube путь
        # сегмента содержит «/playlist/index.m3u8/…», но по нему отдаются
        # байты видео. Поэтому смотрим содержимое ответа.
        probed = self._probe_playlist(media_url)
        if probed is None:
            return None
        raw, final_url = probed

        # master с несколькими качествами собираем сами, если источник
        # отдаёт каждое качество отдельным потоком
        master = build_master_playlist(target)
        if master:
            return master, media_url
        return raw, final_url

    def _rewrite_playlist(self, raw: str, final_url: str) -> str:
        """Переписать все ссылки плейлиста на наш прокси."""
        base_dir = final_url.rsplit("/", 1)[0] + "/"

        def to_proxy(link: str) -> str:
            link = link.strip()
            if not link:
                return link
            # уже наш прокси (собранные master-плейлисты) — не оборачиваем дважды
            if link.startswith(("/hls?", "/kodik-master?")):
                return link
            absolute = urllib.parse.urljoin(base_dir, link)
            return f"/hls?url={urllib.parse.quote(absolute, safe='')}"

        out_lines: list[str] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                out_lines.append(line)
                continue
            if stripped.startswith("#"):
                # в тегах тоже бывают URI="..." (ключи шифрования, аудио-дорожки)
                if 'URI="' in stripped:
                    stripped = re.sub(
                        r'URI="([^"]+)"',
                        lambda m: 'URI="' + to_proxy(m.group(1)) + '"',
                        stripped,
                    )
                out_lines.append(stripped)
            else:
                out_lines.append(to_proxy(stripped))
        return "\n".join(out_lines)

    def _serve_static(self, path: Path) -> None:
        """Отдать статический файл (hls.js) с кешированием."""
        data = path.read_bytes()
        mime = {
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".map": "application/json",
        }.get(path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _serve_subtitles(self, url: str) -> None:
        """Отдать .vtt из интернета или локального файла, конвертируя srt при необходимости."""
        try:
            if url.startswith(("http://", "https://")):
                req = urllib.request.Request(url, headers=dict(BROWSER_HEADERS))
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = r.read()
            else:
                candidate = (Path("./videos") / url).resolve()
                candidate.relative_to(Path("./videos").resolve())
                raw = candidate.read_bytes()
        except Exception as e:  # noqa: BLE001
            return self._send_text(404, f"<h3>Субтитры не найдены</h3><p>{e}</p>")

        text = raw.decode("utf-8-sig", errors="replace")
        if not text.lstrip().upper().startswith("WEBVTT"):
            # простейшая конвертация SRT -> VTT
            text = "WEBVTT\n\n" + text.replace("\r\n", "\n").replace(",", ".")
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/vtt; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle_yt(self, url: str) -> None:
        """YouTube: всегда через локальный кеш — ссылки протухают за ~6 ч."""
        try:
            fmts = yt_formats(url)
        except Exception as e:  # noqa: BLE001
            return self._send_text(500, f"<h3>yt-dlp не смог разобрать ссылку</h3><p>{e}</p>")
        target = pick_direct(fmts)
        if not target:
            return self._send_text(
                409,
                "<h3>У YouTube нет целого mp4 (только video-only + audio-only)</h3>"
                "<p>Используйте мод Media Player V (он сам склеивает потоки) или скачайте "
                "видео через download_and_prepare.py и раздайте через server.py.</p>",
            )
        local = cache.get_or_fetch(target)
        return self._serve_local_file(local)

    def _handle_resolve(self, url: str, short: bool = False) -> None:
        info = resolve_input(url)
        kind = info["kind"]
        if kind == "direct":
            direct_url = info["url"]
            ctype = (info.get("content_type") or "").lower()

            # ВАЖНО: <video> может принять только реальный видеофайл.
            # Если это HLS/DASH-плейлист или HTML — CEF выдаст ошибку 4.
            if self._is_playlist_or_html(direct_url, ctype):
                return self._convert_to_file(url, direct_url, ctype)

            # «Протухающие» ссылки (googlevideo) всегда в кеш, остальные — потоком.
            if "googlevideo.com" in direct_url:
                try:
                    local = cache.get_or_fetch(direct_url)
                    return self._serve_local_file(local)
                except Exception as e:  # noqa: BLE001
                    return self._send_text(502, f"<h3>Не удалось скачать в кеш</h3><p>{e}</p>")
            return self._stream_remote(direct_url)
        if kind == "hls":
            try:
                local = remux_to_local(info["url"], cache._build_filename(url))
            except Exception as e:  # noqa: BLE001
                return self._send_text(500, f"<h3>Не удалось собрать HLS-поток</h3><p>{e}</p>")
            return self._serve_local_file(local)
        # page без прогрессивного формата
        return self._send_text(
            409,
            "<h3>Только раздельные потоки (video-only + audio-only)</h3>"
            "<p>CEF такое не проигрывает. Варианты:</p>"
            "<ul><li>используйте мод Media Player V — он сам склеивает потоки;</li>"
            "<li>скачайте видео через download_and_prepare.py "
            "и раздайте через server.py.</li></ul>",
        )

    def _index(self) -> None:
        rows = (
            "".join(
                f"<li><a href='/p/{rid}'><code>/p/{rid}</code></a> — {u[:90]}</li>"
                for rid, u in REGISTRY.items()
            )
            or "<li>Пока пусто. Добавьте ссылку через <code>/resolve?url=...</code></li>"
        )
        html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>GTA 5 Видео-прокси</title>
<style>body{{font-family:system-ui;max-width:860px;margin:40px auto;padding:0 16px}}
code{{background:#f0f0f0;padding:2px 6px;border-radius:4px;word-break:break-all}}
table{{border-collapse:collapse}}
td,th{{border:1px solid #ddd;padding:6px 10px;text-align:left}}</style>
</head><body>
<h1>🎬 Видео-прокси для GTA 5</h1>
<p>Превращает обычные ссылки на видео в прямые ссылки для телевизора в игре.</p>
<table>
<tr><th>Что нужно</th><th>Ссылка</th></tr>
<tr><td>YouTube</td><td><code>/yt?url=https://www.youtube.com/watch?v=...</code></td></tr>
<tr><td>Любой сайт</td><td><code>/r?url=https://...</code></td></tr>
<tr><td>Прямой файл (прокси)</td><td><code>/get?url=https://site.com/a.mp4</code></td></tr>
<tr><td>Инфо о ссылке (JSON)</td><td><code>/resolve?url=https://...</code></td></tr>
<tr><td>Локальный файл из videos/</td><td><code>/local/имя.mp4</code></td></tr>
</table>
<h2>Зарегистрированные короткие ссылки</h2>
<ul>{rows}</ul>
</body></html>"""
        self._send_text(200, html)


def main() -> None:
    ap = argparse.ArgumentParser(description="GTA 5 Video Proxy: обычные ссылки → прямые")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    load_registry()
    # Тихая уборка: скачанные видео и профили браузера старее суток.
    # Импорт абсолютный от пакета: плоский `import cleanup` внутри пакета
    # не находится, ошибка проглатывалась и уборка не запускалась вовсе.
    try:
        from .infra import cleanup  # noqa: PLC0415

        cleanup.auto_clean()
    except Exception as e:  # noqa: BLE001
        diag.log("err", "автоуборка не запустилась", error=f"{type(e).__name__}: {e}"[:200])
    ProxyHandler.proxy_base = f"http://127.0.0.1:{args.port}"

    class _Server(ThreadingHTTPServer):
        daemon_threads = True  # не держать процесс из-за клиента
        allow_reuse_address = True
        request_queue_size = 128  # hls.js открывает до 6 соединений сразу

    server = _Server((args.host, args.port), ProxyHandler)

    print("=" * 66)
    print("🎬 GTA 5 Видео-прокси запущен")
    print(f"   Оглавление  : http://127.0.0.1:{args.port}/")
    print(f"   YouTube     : http://127.0.0.1:{args.port}/yt?url=ССЫЛКА")
    print(f"   Любой сайт  : http://127.0.0.1:{args.port}/r?url=ССЫЛКА")
    print(f"   JSON-инфо   : http://127.0.0.1:{args.port}/resolve?url=ССЫЛКА")
    print("   Остановка   : Ctrl+C")
    print("=" * 66)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        for p in list(_ffmpeg_procs):
            p.kill()


if __name__ == "__main__":
    main()
