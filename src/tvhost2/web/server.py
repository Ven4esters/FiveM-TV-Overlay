"""HTTP-сервер: единственный вход для игры и оверлея.

Маршруты (короткие и по одному смыслу на каждый):

    GET  /new?url=...         создать сессию, вернуть ссылку для игры
    GET  /player/<sid>        страница плеера (её открывает телевизор)
    GET  /stream/<sid>        master-плейлист через мост
    GET  /hls?url=...         мост: плейлист или сегмент источника
    GET  /cmd/<sid>?since=    плеер забирает команды пульта
    POST /control/<sid>?cmd=  пульт отправляет команду плееру
    POST /report/<sid>        плеер сообщает состояние
    GET  /state/<sid>         оверлей спрашивает состояние
    GET  /loudness/<sid>      усиление для выравнивания громкости
    GET  /sessions            список активных сессий
    GET  /sources             состояние источников (вкладка «Источники»)
    GET  /static/<file>       hls.min.js и прочие файлы страницы

ГЛАВНОЕ ПРАВИЛО (цена — ERR-081): любой сбой превращается в HTTP-ответ с
причиной. Обрыв соединения hls.js видит как необъяснимую сетевую ошибку и
не может переключиться на другое качество.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import sys
import threading
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..audio import loudness
from ..core import diagnostics as diag
from ..core import registry
from ..sources import register_all
from . import hls as hls_bridge
from . import player as player_page
from . import resolver
from .fetch import FetchError, fetch
from .session import SessionStore

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
HLS_JS_NAME = "hls.min.js"
MAX_COMMANDS = 200
"""Предел очереди команд: если плеер не отвечает час, очередь не растёт."""


class SessionRuntime:
    """То, что нужно сессии в рантайме, но не является её состоянием.

    Отдельно от `Session`: команды и отчёт — это про конкретное соединение
    с плеером, а не про видео. При «Повторить» видео сохраняется, а очередь
    команд должна очиститься.
    """

    def __init__(self) -> None:
        self.commands: deque[dict[str, Any]] = deque(maxlen=MAX_COMMANDS)
        self.seq = 0
        self.last_report: dict[str, Any] = {}
        self.lock = threading.Lock()

    def push(self, cmd: str, value: Any) -> int:
        with self.lock:
            self.seq += 1
            self.commands.append({"seq": self.seq, "cmd": cmd, "value": value})
            return self.seq

    def since(self, seq: int) -> tuple[int, list[dict[str, Any]]]:
        with self.lock:
            return self.seq, [c for c in self.commands if c["seq"] > seq]


class AppState:
    """Всё изменяемое состояние процесса в одном месте."""

    def __init__(self) -> None:
        self.sessions = SessionStore()
        self.runtime: dict[str, SessionRuntime] = {}
        self.loudness = loudness.LoudnessCache(Path("videos/cache/loudness.json"))
        self.loudness_lock = threading.Lock()

    def runtime_for(self, sid: str) -> SessionRuntime:
        if sid not in self.runtime:
            self.runtime[sid] = SessionRuntime()
        return self.runtime[sid]

    def drop(self, sid: str) -> None:
        self.runtime.pop(sid, None)
        self.sessions.drop(sid)


STATE = AppState()


class OverlayHandler(BaseHTTPRequestHandler):
    """Обработчик запросов игры и оверлея.

    Класс верхнего уровня, а не вложенный: вложенный делает `make_handler`
    монстром сложности 71 (все ветки всех методов считаются его ветками).
    Адрес прокси приходит параметром класса через `make_handler`.
    """

    server_version = "FiveMVideoOverlay/2.0"
    protocol_version = "HTTP/1.0"
    proxy_base = "http://127.0.0.1:9001"

    # ---------------- утилиты ответов ----------------
    def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", cache)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # плеер закрыл вкладку — это не ошибка

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self._send(status, body, "application/json; charset=utf-8")

    def _playlist(self, body: bytes, status: int = 200) -> None:
        """Плейлист отдаём с CORS и без кеша: токены CDN меняются."""
        self._send(status, body, "application/vnd.apple.mpegurl")

    def _error_playlist(self, reason: str, status: int = 502) -> None:
        """Сбой источника — валидным плейлистом с причиной (ERR-081)."""
        self._playlist(hls_bridge.error_playlist(reason), status)

    # ---------------- разбор запроса ----------------
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            self._route_get(path, query)
        except Exception as exc:  # последний рубеж: никогда не рвать молча
            log.exception("необработанный сбой на %s", path[:80])
            if path.startswith(("/hls", "/stream")):
                self._error_playlist(f"внутренний сбой: {type(exc).__name__}")
            elif path.startswith(("/player", "/source")):
                self._send(
                    500, _html_error("Внутренний сбой", str(exc)), "text/html; charset=utf-8"
                )
            else:
                self._json(500, {"error": str(exc)})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            self._route_post(parsed.path, query)
        except Exception as exc:  # последний рубеж
            log.exception("необработанный сбой POST %s", parsed.path[:80])
            self._json(500, {"error": str(exc)})

    # ---------------- маршруты ----------------
    def _route_get(self, path: str, query: dict[str, list[str]]) -> None:
        """Таблица маршрутов: точные адреса, затем префиксы.

        Лестница if/elif на 12 ветвей давала сложность 73 при пределе 15.
        Таблица читается сверху вниз и не растёт сложностью при
        добавлении маршрута.
        """
        exact = {
            "/": self._index,
            "/index.html": self._index,
            "/sessions": self._list_sessions,
            "/sources": self._list_sources,
            "/diag": self._diag,
            "/hls": self._bridge,
        }
        handler = exact.get(path)
        if handler is not None:
            if path in ("/", "/index.html", "/sessions", "/sources"):
                return handler()
            return handler(query)
        prefixes = (
            ("/player/", self._player_page),
            ("/stream/", self._stream),
            ("/cmd/", self._commands),
            ("/state/", self._state),
            ("/loudness/", self._loudness),
            ("/static/", self._static),
            ("/subs/", self._subtitle),
        )
        for prefix, method in prefixes:
            if path.startswith(prefix):
                tail = path[len(prefix) :].strip("/")
                if prefix in ("/cmd/", "/subs/"):
                    return method(tail, query)
                return method(tail)
        if path == "/new":
            return self._create_session(query)
        self._json(404, {"error": f"нет маршрута {path}"})
        return None

    def _index(self) -> None:
        self._json(200, {"app": "FiveM Video Overlay", "version": "2.0"})

    def _list_sessions(self) -> None:
        items = [
            {"sid": s.sid, "title": s.title, "source": s.source_name} for s in STATE.sessions.all()
        ]
        self._json(200, {"sessions": items})

    def _list_sources(self) -> None:
        register_all()
        self._json(200, {"sources": registry.status_table()})

    def _diag(self, query: dict[str, list[str]]) -> None:
        """Журнал работы для вкладки «Журнал».

        limit ограничен: отдавать 4000 строк в окно через HTTP бессмысленно,
        человек всё равно смотрит хвост.
        """
        try:
            limit = min(int((query.get("limit") or ["300"])[0]), 1000)
        except ValueError:
            limit = 300
        self._json(
            200, {"text": diag.JOURNAL.tail_text(limit), "entries": diag.JOURNAL.tail_json(limit)}
        )

    def _route_post(self, path: str, query: dict[str, list[str]]) -> None:
        if path.startswith("/control/"):
            self._control(path[len("/control/") :].strip("/"), query)
            return
        if path.startswith("/report/"):
            self._report(path[len("/report/") :].strip("/"))
            return
        if path.startswith("/trans/"):
            self._translation(path[len("/trans/") :].strip("/"), query)
            return
        self._json(404, {"error": f"нет маршрута {path}"})

    # ---------------- сессии ----------------
    def _create_session(self, query: dict[str, list[str]]) -> None:
        url = (query.get("url") or [""])[0].strip()
        if not url:
            return self._json(400, {"error": "нужен параметр url"})
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            resolved = resolver.resolve(url)
        except resolver.ResolveError as exc:
            return self._json(
                200,
                {"ok": False, "reason": exc.message, "hint": exc.hint},
            )

        session = STATE.sessions.create(
            url, title=(query.get("title") or [""])[0] or resolved.stream.title
        )
        session.stream = resolved.stream
        session.source_name = resolved.source_name
        STATE.runtime_for(session.sid)
        log.info("сессия %s: %s → %s", session.sid, resolved.source_name, url[:60])
        return self._json(
            200,
            {
                "ok": True,
                "sid": session.sid,
                "player": f"{self.proxy_base}/player/{session.sid}",
                "control": f"{self.proxy_base}/control/{session.sid}",
                "source": resolved.source_name,
                "title": session.title,
                "translations": [t.get("title", "") for t in resolved.stream.audio_tracks],
                "translation": session.translation_title,
                "warning": "",
            },
        )

    def _player_page(self, sid: str) -> None:
        session = STATE.sessions.get(sid)
        if session is None:
            self._send(
                404,
                _html_error("Сессия не найдена", "Создайте новую в оверлее."),
                "text/html; charset=utf-8",
            )
        page = player_page.render(
            session, self.proxy_base, f"{self.proxy_base}/static/{HLS_JS_NAME}"
        )
        self._send(200, page.encode(), "text/html; charset=utf-8")

    def _subtitle(self, sid: str, query: dict[str, list[str]]) -> None:
        """Одна дорожка субтитров через мост: `/subs/<sid>?i=0`.

        Почему через прокси, а не прямой ссылкой: у YouTube субтитры отдаются
        с токеном и требуют заголовков источника, а браузер игры может не
        иметь доступа к чужому домену. К нашему адресу доступ есть всегда.
        """
        session = STATE.sessions.get(sid)
        if session is None or session.stream is None:
            return self._json(404, {"error": "сессия не найдена"})
        tracks = session.stream.subtitles
        if not tracks:
            return self._json(404, {"error": "у потока нет субтитров"})
        try:
            index = int((query.get("i") or ["0"])[0])
        except ValueError:
            index = 0
        if not 0 <= index < len(tracks):
            return self._json(404, {"error": "нет такой дорожки"})
        target = str(tracks[index].get("url") or "")
        try:
            response = fetch(target, _referer_headers(target), max_bytes=4 * 1024 * 1024)
        except FetchError as exc:
            return self._json(exc.status, {"error": exc.reason})
        # VTT отдаём именно как VTT: браузер ждёт этот тип, иначе не покажет
        return self._send(200, response.body, "text/vtt; charset=utf-8")

    def _state(self, sid: str) -> None:
        session = STATE.sessions.get(sid)
        if session is None:
            return self._json(404, {"error": "сессия не найдена"})
        state = session.state()
        state.update(STATE.runtime_for(sid).last_report)
        return self._json(200, state)

    # ---------------- управление ----------------
    def _control(self, sid: str, query: dict[str, list[str]]) -> None:
        session = STATE.sessions.get(sid)
        if session is None:
            return self._json(404, {"error": "сессия не найдена"})
        cmd = (query.get("cmd") or [""])[0]
        if not cmd:
            return self._json(400, {"error": "нужен параметр cmd"})
        raw = (query.get("value") or [None])[0]
        value: Any = raw
        if raw is not None:
            low = raw.lower()
            if low in ("true", "1", "on"):
                value = True
            elif low in ("false", "0", "off"):
                value = False
            else:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw
        if cmd == "normalize":
            session.normalize = bool(value)
        elif cmd == "volume" and isinstance(value, float):
            session.volume = value
        elif cmd == "mute":
            session.muted = bool(value)
        seq = STATE.runtime_for(sid).push(cmd, value)
        return self._json(200, {"ok": True, "seq": seq, "cmd": cmd, "value": value})

    def _commands(self, sid: str, query: dict[str, list[str]]) -> None:
        if STATE.sessions.get(sid) is None:
            return self._json(404, {"error": "сессия не найдена"})
        try:
            since = int((query.get("since") or ["0"])[0])
        except ValueError:
            since = 0
        seq, commands = STATE.runtime_for(sid).since(since)
        return self._json(200, {"seq": seq, "commands": commands})

    def _report(self, sid: str) -> None:
        if STATE.sessions.get(sid) is None:
            return self._json(404, {"error": "сессия не найдена"})
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "битый JSON"})
        STATE.runtime_for(sid).last_report = data
        return self._json(200, {"ok": True})

    def _translation(self, sid: str, query: dict[str, list[str]]) -> None:
        session = STATE.sessions.get(sid)
        if session is None:
            return self._json(404, {"error": "сессия не найдена"})
        try:
            index = int((query.get("index") or ["0"])[0])
        except ValueError:
            return self._json(400, {"error": "index должен быть числом"})
        tracks = session.stream.audio_tracks if session.stream else ()
        if not tracks or not (0 <= index < len(tracks)):
            return self._json(400, {"error": "нет такой озвучки"})
        session.translation_index = index
        try:
            resolved = resolver.resolve(session.url, translation_index=index)
        except resolver.ResolveError as exc:
            return self._json(200, {"ok": False, "reason": exc.message})
        session.stream = resolved.stream
        STATE.runtime_for(sid).commands.clear()
        return self._json(
            200,
            {"ok": True, "translation": session.translation_title, "sid": session.sid},
        )

    # ---------------- поток и мост ----------------
    def _stream(self, sid: str) -> None:
        """Master-плейлист сессии через наш мост."""
        session = STATE.sessions.get(sid)
        if session is None or session.stream is None:
            return self._error_playlist("сессия не найдена", 404)
        stream = session.stream
        if not stream.qualities:
            return self._error_playlist("у потока нет качеств", 502)
        # Мастер отдаётся БРАУЗЕРУ напрямую, поэтому ссылки в нём обязаны
        # быть уже нашими. Раньше здесь лежали сырые адреса источника
        # «мост перепишет их сам» — но переписывать их было некому: браузер
        # получал `file:///…` или чужой домен, и поток молча не играл.
        quality_list = [
            (
                q.height,
                q.width,
                q.bitrate_kbps,
                hls_bridge.proxify(q.url, f"{self.proxy_base}/stream/{sid}"),
            )
            for q in stream.qualities
        ]
        audio = (
            hls_bridge.proxify(stream.audio_url, f"{self.proxy_base}/stream/{sid}")
            if stream.audio_url
            else ""
        )
        master = hls_bridge.build_master(quality_list, audio)
        return self._playlist(master.encode())

    def _bridge(self, query: dict[str, list[str]]) -> None:
        """Универсальный мост: плейлист или сегмент источника."""
        target = (query.get("url") or [""])[0]
        if not target:
            return self._error_playlist("нужен параметр url", 400)
        headers = _referer_headers(target)
        try:
            # file:// — склейка ffmpeg (видео+звук): отдаём плейлист и сегменты
            # с диска тем же путём, что и сетевые. Отдельной ветки не нужно:
            # fetch умеет и локальные файлы, а rewrite — абсолютные пути.
            response = fetch(target, headers, max_bytes=8 * 1024 * 1024)
        except FetchError as exc:
            return self._error_playlist(exc.reason, exc.status)

        if not hls_bridge.is_playlist(response.body):
            # сегмент: отдаём как есть, без попыток «переписать»
            ctype = response.content_type or "video/mp2t"
            return self._send(200, response.body, ctype, cache="no-store")

        text = response.text()
        rewritten = hls_bridge.rewrite(text, response.final_url, "/hls")
        return self._playlist(rewritten.encode())

    # ---------------- громкость ----------------
    def _loudness(self, sid: str) -> None:
        session = STATE.sessions.get(sid)
        if session is None or session.stream is None or session.stream.best is None:
            return self._json(404, {"error": "сессия не найдена"})
        url = session.stream.best.url
        cached = STATE.loudness.get(url)
        if cached is not None:
            return self._json(200, cached.as_dict())
        with STATE.loudness_lock:  # замер тяжёлый: не пускаем параллельно
            cached = STATE.loudness.get(url)
            if cached is None:
                value = loudness.measure(url, _referer_headers(url))
                if value is None:
                    return self._json(200, {"gainDb": None, "note": "замер не удался"})
                STATE.loudness.put(url, value)
                cached = value
        return self._json(200, cached.as_dict())

    # ---------------- статика ----------------
    def _static(self, name: str) -> None:
        # защита от выхода из папки: имя без слешей и точек в начале
        if "/" in name or "\\" in name or name.startswith("."):
            self._json(400, {"error": "недопустимое имя файла"})
            return
        path = STATIC_DIR / name
        if not path.is_file():
            self._json(404, {"error": f"нет файла {name}"})
            return
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), ctype, cache="public, max-age=86400")


def make_handler(base: str) -> type[BaseHTTPRequestHandler]:
    """Обработчик с привязанным адресом прокси.

    Класс, а не экземпляр: ThreadingHTTPServer создаёт обработчик на каждый
    запрос, а адрес прокси нужен всем.
    """

    class _Bound(OverlayHandler):
        proxy_base = base

    return _Bound


def _referer_headers(url: str) -> dict[str, str]:
    """Referer под источник. Kodik подписывает запросы под домен-партнёр."""
    low = url.lower()
    if "solodcdn" in low or "kodik" in low:
        return {"Referer": "https://kodikplayer.com/", "Origin": "https://kodikplayer.com"}
    if "rutube" in low or "rtbcdn" in low:
        return {"Referer": "https://rutube.ru/"}
    if "okcdn" in low or "vkvd" in low or "mycdn" in low:
        return {"Referer": "https://vk.com/"}
    return {}


def _html_error(title: str, hint: str) -> bytes:
    """Страница ошибки для случая, когда ждут HTML, а не JSON."""
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<body style='background:#0F1419;color:#CBD2E4;font:16px/1.6 Segoe UI,Arial;padding:32px'>"
        f"<h2 style='color:#F87171'>{title}</h2><p>{hint}</p>"
        "</body>"
    ).encode()


def serve(host: str = "0.0.0.0", port: int = 9001) -> ThreadingHTTPServer:
    """Поднять сервер. Возвращает его же — вызывающий решает, как крутить."""
    register_all()
    base = f"http://127.0.0.1:{port}"
    handler = make_handler(base)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    log.info("сервер на %s:%s, адрес для игры %s", host, port, base)
    return server


def main() -> None:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="FiveM Video Overlay 2.0 — сервер")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    diag.mark_start()
    server = serve(args.host, args.port)
    sys.stdout.write(f"5M Video Overlay 2.0: http://127.0.0.1:{args.port}/\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
