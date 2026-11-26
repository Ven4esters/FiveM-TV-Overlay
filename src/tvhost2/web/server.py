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
import subprocess
import sys
import threading
import urllib.parse
from collections import OrderedDict, deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import __version__
from ..audio import loudness
from ..core import diagnostics as diag
from ..core import registry, resources
from ..sources import register_all
from . import hls as hls_bridge
from . import player as player_page
from . import resolver
from .fetch import FetchError, fetch, headers_for
from .history import HISTORY
from .session import SessionStore

log = logging.getLogger(__name__)

STATIC_DIR = resources.resource("web", "static")
"""Страница плеера и hls.min.js. В .exe — распакованная копия (resources.py)."""
HLS_JS_NAME = "hls.min.js"
MAX_COMMANDS = 200
HTTP_PARTIAL = 206

SOURCE_TIMEOUT_S = 120
"""Сколько ждём источник. У anidub.com один сегмент идёт до 22 секунд."""
"""Частичный ответ: столько отдаём при перемотке (Range)."""
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


class SegmentCache:
    """Небольшой кэш сегментов и кусков файла: адрес → байты.

    Почему на сервере, а не только в браузере: у сегментов CDN подпись
    одноразовая, и браузер обязан идти заново тем же адресом; отдавая из
    памяти, мы снимаем ровно ту задержку, из-за которой hls.js объявляет
    `fragLoadTimeOut`. Размер ограничен: сегменты весят по 1–2 МБ, сотня
    штук в памяти — это ~100 МБ, поэтому предел 64 МБ и вытеснение самых
    старых.
    """

    LIMIT_BYTES = 64 * 1024 * 1024
    """Сколько памяти разрешено. Больше — уже неприлично для прокси."""

    def __init__(self) -> None:
        self._items: OrderedDict[str, tuple[bytes, str, str]] = OrderedDict()
        self._loading: set[str] = set()
        """Что качается прямо сейчас: второй запрос ждёт, а не дублирует."""
        self._lock = threading.Lock()
        self._size = 0

    def has(self, key: str) -> bool:
        """Есть ли кусок в памяти — дёшево, без копирования."""
        with self._lock:
            return key in self._items

    def busy(self, key: str) -> bool:
        """Этот кусок уже качается другим запросом?

        Зачем: hls.js при `fragLoadTimeOut` повторяет ТОТ ЖЕ адрес. Если
        первый запрос ещё в пути, второй не должен идти к CDN заново — он
        встанет в очередь и получит готовое. Иначе два одинаковых запроса
        конкурируют за канал, и оба упираются в таймаут.
        """
        with self._lock:
            return key in self._loading

    def claim(self, key: str) -> bool:
        """Занять кусок под загрузку. False — уже занят."""
        with self._lock:
            if key in self._loading:
                return False
            self._loading.add(key)
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._loading.discard(key)

    def get(self, key: str) -> tuple[bytes, str, str] | None:
        with self._lock:
            found = self._items.get(key)
            if found is None:
                return None
            self._items.move_to_end(key)
            return found

    def put(self, key: str, body: bytes, ctype: str, extra: str = "") -> None:
        if len(body) > self.LIMIT_BYTES // 4:
            return  # одиночный гигант (полный mp4) в память не кладём
        with self._lock:
            if key in self._items:
                old = self._items.pop(key)[0]
                self._size -= len(old)
            self._items[key] = (body, ctype, extra)
            self._size += len(body)
            while self._size > self.LIMIT_BYTES and self._items:
                _, (dropped, _, _) = self._items.popitem(last=False)
                self._size -= len(dropped)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._size = 0


class AppState:
    """Всё изменяемое состояние процесса в одном месте."""

    def __init__(self) -> None:
        self.sessions = SessionStore()
        self.runtime: dict[str, SessionRuntime] = {}
        self.loudness = loudness.LoudnessCache(Path("videos/cache/loudness.json"))
        self.loudness_lock = threading.Lock()
        self.history = HISTORY
        # Кэш сегментов HLS. Зачем: сегмент Kodik идёт с CDN ~3 с, а hls.js
        # ждёт ответа меньше и объявляет `fragLoadTimeOut` — в журнале
        # заказчика это сплошные таймауты, из-за которых «поток не
        # загрузился». Плеер повторяет запрос того же сегмента — и второй
        # раз он обязан прийти мгновенно.
        self.segments = SegmentCache()

    def runtime_for(self, sid: str) -> SessionRuntime:
        if sid not in self.runtime:
            self.runtime[sid] = SessionRuntime()
        return self.runtime[sid]

    def drop(self, sid: str) -> None:
        self.runtime.pop(sid, None)
        self.sessions.drop(sid)


STATE = AppState()


def _track_title(stream: object, index: int) -> str:
    """Название дорожки по индексу — пустая строка, если такой нет."""
    tracks = getattr(stream, "audio_tracks", ()) or ()
    if 0 <= index < len(tracks):
        return str(tracks[index].get("title") or "").strip()
    return ""


def _keep_translation(session: Any, url: str, number: int, resolved: Any) -> Any:
    """Открыть ту же СТУДИЮ, что человек выбрал, а не ту же строку списка.

    Зачем: у anidub.org состав озвучек РАЗНЫЙ на разных сериях. На 1-й и
    27-й их четыре («AniLiberty», «Субтитры», «Amedia», «Неизвестный»), а на
    500-й — три, и «AniLiberty» там нет вовсе; на 1180-й список снова
    другой. Индекс, который пульт помнил, на новой серии указывает на
    чужого актёра или вылетает за пределы — «нет такой озвучки».

    Ищем по названию. Не нашли — оставляем то, что дал источник, но пульту
    сообщаем правду (он и так подтянет новое имя из /state).
    """
    wanted = str(getattr(session, "translation_wanted", "") or "").strip()
    if not wanted:
        return resolved
    stream = getattr(resolved, "stream", None)
    tracks = getattr(stream, "audio_tracks", ()) or ()
    titles = [str(t.get("title") or "").strip() for t in tracks]
    if wanted in titles:
        found = titles.index(wanted)
        if found == session.translation_index:
            return resolved  # уже та, что нужно
        try:
            again = resolver.resolve(url, translation_index=found, episode=number)
        except resolver.ResolveError:
            return resolved
        session.translation_index = found
        return again
    return resolved


def _looks_like_playlist(url: str) -> bool:
    """Сегмент это или плейлист — по адресу, без скачивания.

    Плейлист m3u8 надо переписать: его сегменты ведут на CDN, и браузер не
    должен ходить туда напрямую. Сегмент достаточно отдать как есть.

    Скачивать весь сегмент ради проверки нельзя: у anidub.com один сегмент
    идёт 22 секунды, и именно из-за этого плеер сдавался (см. `_send_stream`).
    Поэтому смотрим на адрес: `.m3u8`/`manifest` — плейлист, `file://` —
    локальная склейка ffmpeg (тоже плейлист), остальное — сегмент.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "file":
        return True
    path = parts.path.lower()
    if path.endswith((".m3u8", ".m3u")):
        return True
    return "m3u8" in parts.query.lower() or "manifest" in parts.query.lower()


def _open_source(target: str, headers: dict[str, str]):
    """Открыть поток источника для потоковой отдачи.

    Отдельная функция, а не `fetch`: `fetch` читает ВЕСЬ ответ в память —
    для плейлиста это правильно, для сегмента смерти подобно (см.
    `_send_stream`). Таймаут на чтение — по умолчанию у urllib он общий.
    """
    safe_url = urllib.parse.quote(target, safe=":/?&=#%+@!$'()*,;[]~")
    request = urllib.request.Request(safe_url, headers=headers_for(target, headers))
    return urllib.request.urlopen(request, timeout=SOURCE_TIMEOUT_S)


class OverlayHandler(BaseHTTPRequestHandler):
    """Обработчик запросов игры и оверлея.

    Класс верхнего уровня, а не вложенный: вложенный делает `make_handler`
    монстром сложности 71 (все ветки всех методов считаются его ветками).
    Адрес прокси приходит параметром класса через `make_handler`.
    """

    # Версию не хардкодим: она одна на пакет, тег и окно пульта. Иначе
    # в заголовке и в `/` остаётся «2.0», пока весь проект уже 2.1.
    server_version = f"FiveMVideoOverlay/{__version__}"
    protocol_version = "HTTP/1.0"
    proxy_base = "http://127.0.0.1:9001"

    def log_message(self, format: str, *args: object) -> None:
        """Не писать строку запроса в stderr — в сборке его НЕТ.

        Это не косметика, а причина, по которой .exe вообще не отвечал.
        Цепочка такая:

            send_response()  →  log_request()  →  log_message()
                             →  sys.stderr.write(...)

        В сборке PyInstaller с `console=False` у процесса нет консоли, и
        `sys.stderr` там **None**. Значит `sys.stderr.write` — это
        `AttributeError: 'NoneType' object has no attribute 'write'`, причём
        возникает он ВНУТРИ `send_response`, то есть до отправки заголовков.
        Клиент видит «Remote end closed connection without response», а
        сервер при этом жив и порт слушает — ровно то, что показывал отчёт
        (порт слушает, ответа нет).

        Заодно решается вторая проблема: `ThreadingHTTPServer` печатает
        трейсбеки в тот же отсутствующий stderr, поэтому сбой был невидим.

        Запросы к серверу пишутся в собственный журнал (`diagnostics`), им
        этого вывода и достаточно: подробный лог каждого запроса в окне
        «Журнал» только мешал бы.
        """

    def handle_one_request(self) -> None:
        """Любая ошибка разбора запроса попадает в журнал, а не в небытие.

        Штатный `handle_one_request` печатает трейсбек через
        `socketserver`, а тот пишет в `sys.stderr`. В сборке его нет —
        и сбой обработчика исчезал бесследно. Здесь трейсбек уходит в
        журнал, поэтому причина видна и в окне, и в файле рядом с .exe.
        """
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Клиент ушёл — это НЕ сбой сервера. На Windows то же самое
            # приходит как `ConnectionAbortedError` (WinError 10053) и
            # раньше писалось в журнал как «сбой при разборе запроса»:
            # страница плеера перезагружается (смена серии, озвучки) или
            # просто закрывается вкладка — и в журнале появлялась красная
            # ошибка там, где всё в порядке. Журнал на таких случаях
            # молчит, а настоящие отказы видны по-прежнему.
            self.close_connection = True
        except Exception as exc:
            log.exception("сбой при разборе запроса")
            diag.log(
                "err",
                f"сбой при разборе запроса: {type(exc).__name__}: {exc}",
            )
            self.close_connection = True

    # ---------------- утилиты ответов ----------------
    def _send_stream(
        self,
        target: str,
        headers: dict[str, str],
        ctype: str,
        cache_key: str = "",
    ) -> None:
        """Отдать сегмент ПО МЕРЕ скачивания, а не после.

        Зачем: сегмент anidub.com (3 МБ) отдаётся источником **22 секунды**,
        и пока мы копили его в память целиком, клиент успевал сдаться по
        своему таймауту. Отсюда `fragLoadError HTTP 502` и «серия не
        запускается»: hls.js считал сегмент битым, повторял запрос, забивал
        канал — и следующие серии получали ту же ошибку.

        Теперь байты идут сразу, как приходят: браузер начинает разбор
        первого куска, не дожидаясь конца файла.
        """
        try:
            with _open_source(target, headers) as response:
                size = response.headers.get("Content-Length")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                if size:
                    self.send_header("Content-Length", size)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                collected = bytearray()
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    if self.command != "HEAD":
                        self.wfile.write(chunk)
                    if cache_key and len(collected) < 32 * 1024 * 1024:
                        collected.extend(chunk)
                if cache_key and collected and size:
                    STATE.segments.put(cache_key, bytes(collected), ctype)
        except Exception as exc:  # обрыв на полпути — это норма, не сбой
            log.debug("потоковая отдача прервалась: %s", exc)

    def _send(
        self,
        status: int,
        body: bytes,
        ctype: str,
        cache: str = "no-store",
        extra: dict[str, str] | None = None,
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", cache)
            # Accept-Ranges обязателен: без него браузер считает файл
            # неразмеченным и не пробует перематывать вовсе.
            self.send_header("Accept-Ranges", "bytes")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # плеер закрыл вкладку — это не ошибка

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self._send(status, body, "application/json; charset=utf-8")

    def _playlist(self, body: bytes, status: int = 200) -> None:
        """Плейлист отдаём с CORS и без кеша: токены CDN меняются.

        `no-store` в `_send` уже стоит, но CEF в игре его иногда игнорирует
        и берёт ответ из памяти — тогда после смены серии играет прежняя
        (жалоба «разные серии приводят к одной»). Добавляем то, что старые
        движки понимают без оговорок.
        """
        self._send(
            status,
            body,
            "application/vnd.apple.mpegurl",
            cache="no-store, no-cache, must-revalidate, max-age=0",
            extra={"Pragma": "no-cache", "Expires": "0"},
        )

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
        # Точные адреса: одним параметры запроса не нужны, другим нужны.
        # Таблицы разные, потому что и сигнатуры разные — так это видно
        # и человеку, и mypy, без проверки «а этот метод берёт query?».
        exact: dict[str, Callable[[], None]] = {
            "/": self._index,
            "/index.html": self._index,
            "/sessions": self._list_sessions,
            "/sources": self._list_sources,
            "/history": self._history_list,
        }
        plain = exact.get(path)
        if plain is not None:
            return plain()

        exact_with_query: dict[str, Callable[[dict[str, list[str]]], None]] = {
            "/diag": self._diag,
            "/diag/ffmpeg": self._diag_ffmpeg,
            "/hls": self._bridge,
        }
        with_query = exact_with_query.get(path)
        if with_query is not None:
            return with_query(query)
        # Префиксы разложены по двум таблицам, а не по одной с проверкой
        # «а этот метод берёт query?»: раньше список проверялся строкой,
        # и mypy справедливо не мог понять, какие аргументы передаются.
        # Теперь видно: этим нужен только хвост адреса…
        sid_routes: tuple[tuple[str, Callable[[str], None]], ...] = (
            ("/player/", self._player_page),
            ("/stream/", self._stream),
            ("/state/", self._state),
            ("/loudness/", self._loudness),
            ("/static/", self._static),
        )
        for prefix, sid_method in sid_routes:
            if path.startswith(prefix):
                return sid_method(path[len(prefix) :].strip("/"))
        # …а этим ещё и параметры запроса.
        sid_query_routes: tuple[tuple[str, Callable[[str, dict[str, list[str]]], None]], ...] = (
            ("/cmd/", self._commands),
            ("/subs/", self._subtitle),
        )
        for prefix, query_method in sid_query_routes:
            if path.startswith(prefix):
                return query_method(path[len(prefix) :].strip("/"), query)
        if path == "/new":
            return self._create_session(query)
        self._json(404, {"error": f"нет маршрута {path}"})
        return None

    def _index(self) -> None:
        self._json(200, {"app": "FiveM Video Overlay", "version": __version__})

    def _list_sessions(self) -> None:
        items = [
            {"sid": s.sid, "title": s.title, "source": s.source_name} for s in STATE.sessions.all()
        ]
        self._json(200, {"sessions": items})

    def _close_session(self, sid: str) -> None:
        """Закрыть сессию: убрать и состояние, и очередь команд плеера.

        Без этого маршрута закрыть просмотр было нечем — сессии жили до
        шести часов и копились. Замеру памяти этот маршрут нужен, чтобы
        измерить «до» и «после», а не гадать по графику.
        """
        STATE.drop(sid)
        return self._json(200, {"ok": True, "closed": sid})

    def _history_list(self) -> None:
        """Последние просмотры для пульта: до пяти, свежие сверху."""
        return self._json(200, {"items": STATE.history.as_list()})

    def _history_forget(self, sid: str, query: dict[str, list[str]]) -> None:
        """Забыть просмотр. Ключ — ссылка, а не sid: история не про сессии."""
        url = (query.get("url") or [""])[0] or urllib.parse.unquote(sid)
        if not url:
            return self._json(400, {"error": "нужен параметр url"})
        removed = STATE.history.forget(url)
        return self._json(200, {"ok": removed, "items": STATE.history.as_list()})

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

    def _route_post(  # noqa: PLR0911 — плоский список маршрутов читается лучше
        self, path: str, query: dict[str, list[str]]
    ) -> None:
        if path.startswith("/control/"):
            self._control(path[len("/control/") :].strip("/"), query)
            return
        if path.startswith("/trace/"):
            self._trace(path[len("/trace/") :].strip("/"))
            return
        if path.startswith("/report/"):
            self._report(path[len("/report/") :].strip("/"))
            return
        if path.startswith("/trans/"):
            self._translation(path[len("/trans/") :].strip("/"), query)
            return
        if path.startswith("/episode/"):
            self._episode(path[len("/episode/") :].strip("/"), query)
            return
        # Сравнение ДОБАВЛЯЕТ слэш: адрес приходит как "/history/forget",
        # без хвоста. Со слэшем маршрут не срабатывал и отвечал 404 —
        # нашлось живой проверкой, тест на исходнике этого не видел.
        if path.rstrip("/") == "/history/forget" or path.startswith("/history/forget/"):
            tail = path[len("/history/forget") :].strip("/")
            self._history_forget(tail, query)
            return
        if path.startswith("/close/"):
            self._close_session(path[len("/close/") :].strip("/"))
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
        # В историю — ПОСЛЕ успешного разбора: если сайт не поддержан или
        # поток не отдался, запоминать нечего, возвращаться некуда.
        STATE.history.remember(
            url,
            title=session.title,
            source=resolved.source_name,
            translation=session.translation_title,
        )
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
                # Серии — сразу, а не только в /state. Раньше ответ содержал
                # лишь озвучки, и до первого опроса (800 мс, а на медленном
                # разборе дольше) поле серии оставалось пустым: человек
                # видел серую подсказку «серия» и считал её значением.
                "episodes": [
                    str(e.get("title") or e.get("episode") or "")
                    for e in (resolved.stream.episodes or ())
                ],
                "episode": resolved.stream.episode or 0,
                "episodeSeason": resolved.stream.episode_season or 0,
                "canSwitchEpisode": bool(resolved.stream.can_switch_episode),
                "warning": "",
            },
        )

    def _player_page(self, sid: str) -> None:
        session = STATE.sessions.get(sid)
        if session is None:
            # return обязателен: без него код шёл дальше и звал render(None),
            # что падало уже внутри рендера — с трейсбеком вместо страницы.
            self._send(
                404,
                _html_error("Сессия не найдена", "Создайте новую в оверлее."),
                "text/html; charset=utf-8",
            )
            return
        # Новая загрузка страницы = чистая доска для команд. Плеер в игре
        # начинает опрос с нуля (`lastSeq = 0`), и БЕЗ сброса он получал всю
        # очередь заново — включая старые `mute=true` и `pause`, из-за
        # которых видео «само вставало на паузу» и «теряло звук» после
        # каждого F5 и после смены серии. Команда, отправленная ПОСЛЕ
        # отдачи страницы, доедет как обычно: очередь наполняется уже поверх
        # пустой.
        runtime = STATE.runtime_for(sid)
        with runtime.lock:
            runtime.commands.clear()
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
        raw: str | None = (query.get("value") or [None])[0]  # type: ignore[list-item]
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

    def _trace(self, sid: str) -> None:
        """Плеер сообщает, что получил команду. Строка уходит в журнал.

        Зачем отдельный маршрут, а не общий `/report`: отчёт приходит дважды
        в секунду, а трассировка — только по событиям. Смешав их, мы бы
        утопили журнал отчётами.
        """
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "битый JSON"})
        text = str(data.get("text") or "").strip()
        if text:
            diag.log("player", f"[{sid}] {text[:300]}")
        return self._json(200, {"ok": True})

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
        # Какую озвучку человек выбрал — ЗАПОМИНАЕМ ПО НАЗВАНИЮ, а не по
        # номеру строки. У anidub.org состав списка разный от серии к серии
        # (на 500-й «AniLiberty» нет вовсе, на 1180-й — сплошь другие
        # студии), и индекс «3» на новой серии означает уже другую студию.
        # Отсюда «нет такой озвучки» при переключении: индекс выходил за
        # пределы укоротившегося списка.
        wanted_title = str(tracks[index].get("title") or "").strip()
        session.translation_index = index
        diag.log(
            "ui",
            f"[{sid}] запрос озвучки {index} "
            f"({tracks[index].get('title') if index < len(tracks) else '?'}), "
            f"серия {(session.stream.episode if session.stream else 0)}",
        )
        # Номер серии ОБЯЗАТЕЛЕН при повторном разборе. Без него источник
        # начинает с первой серии: у anidub.org серия в адрес не пишется
        # (`url_with_episode` у него нет), и после смены озвучки человек
        # оказывался на 1-й серии — жалоба «серии нельзя переключить,
        # откидывает на первую». Kodik спасал тем, что хранит номер в
        # `?episode=` самой ссылки.
        current_episode = session.stream.episode if session.stream else 0
        try:
            resolved = resolver.resolve(
                session.url,
                translation_index=index,
                episode=current_episode or None,
            )
        except resolver.ResolveError as exc:
            return self._json(200, {"ok": False, "reason": exc.message})
        session.stream = resolved.stream
        # Что РЕАЛЬНО включилось после разбора.
        got_title = _track_title(session.stream, session.translation_index)
        session.translation_wanted = got_title or wanted_title
        # Если человек просил студию, а её на этой серии нет — говорим прямо.
        # Раньше сервер молча оставлял чужую озвучку, а пульт показывал
        # «нет такой озвучки» без объяснений: человек не понимал, что дело
        # не в сбое, а в том, что на этой серии такой студии просто нет.
        missing = ""
        if wanted_title and got_title and got_title != wanted_title:
            missing = (
                f"на этой серии нет озвучки «{wanted_title}» — включена «{got_title}»"
            )
        STATE.runtime_for(sid).commands.clear()
        # Озвучка — часть просмотра: «Ван-Пис в дубляже» и «в субтитрах»
        # это разные записи, к каждой хочется вернуться своей.
        STATE.history.remember(
            session.url,
            title=session.title,
            source=session.source_name,
            translation_index=index,
            translation=session.translation_title,
        )
        return self._json(
            200,
            {
                "ok": True,
                "translation": session.translation_title,
                "warning": missing,
                "sid": session.sid,
            },
        )

    def _episode(self, sid: str, query: dict[str, list[str]]) -> None:
        """Переключить серию: `POST /episode/<sid>?index=3`.

        Почему отдельный маршрут, а не `cmd`: серия — это НЕ команда плеера.
        И у Kodik, и у anidub.org каждая серия — отдельный поток со своими
        идентификаторами, поэтому «переключить дорожку» физически нечего:
        нужен повторный разбор ссылки. Плееру остаётся перезагрузить
        страницу.

        Механика ОБЩАЯ, без знания про конкретный сайт:

          * номер уходит в `resolver.resolve(..., episode=N)`;
          * источник, который серии не умеет, параметр просто не принимает,
            и разбор идёт как обычно (серия тогда не меняется — и пульт об
            этом честно говорит);
          * ссылку правит `session.episode_url` — источник сам решает, как
            запомнить номер (Kodik кладёт `?episode=N`, остальным не нужно).
        """
        session = STATE.sessions.get(sid)
        if session is None:
            return self._json(404, {"error": "сессия не найдена"})
        try:
            number = int((query.get("index") or ["0"])[0])
        except ValueError:
            return self._json(400, {"error": "index должен быть числом"})
        if number < 1:
            return self._json(400, {"error": "серия нумеруется с 1"})
        if session.stream is not None and not session.stream.can_switch_episode:
            return self._json(
                200,
                {
                    "ok": False,
                    "reason": (
                        "этот источник не отдаёт список серий — серию нужно выбрать на сайте"
                    ),
                },
            )
        url = session.episode_url(number)
        diag.log("ui", f"[{sid}] запрос серии {number}: разбираю {url[:90]}")
        # Ту же СТУДИЮ, что человек выбрал, а не тот же номер строки. У
        # anidub.org состав озвучек разный от серии к серии, и после
        # переключения номера индекс указывал уже на другого актёра или
        # вылетал за пределы списка — отсюда «нет такой озвучки» и не та
        # озвучка после смены серии.
        try:
            resolved = resolver.resolve(
                url, translation_index=session.translation_index, episode=number
            )
        except resolver.ResolveError as exc:
            diag.log("err", f"[{sid}] серия {number} не открылась: {exc.message}")
            return self._json(200, {"ok": False, "reason": exc.message})
        # Если просили конкретную студию, а после разбора список стал
        # другим — доискиваем её по названию и, найдя, разбираем заново.
        resolved = _keep_translation(session, url, number, resolved)
        session.url = url
        session.stream = resolved.stream
        session.title = resolved.stream.title
        STATE.runtime_for(sid).commands.clear()
        STATE.history.remember(
            url,
            title=session.title,
            source=session.source_name,
            translation_index=session.translation_index,
            translation=session.translation_title,
        )
        return self._json(
            200,
            {
                "ok": True,
                "episode": resolved.stream.episode or number,
                "title": session.title,
                "sid": session.sid,
            },
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

    def _stream_headers(self, target: str) -> dict[str, str]:
        """Заголовки сессии, к которой относится этот адрес.

        Ищем по совпадению хоста с качествами/аудио потоков живых сессий.
        Дорого? Сессий единицы, сравнение строковое — дешевле, чем хранить
        в адресе моста ещё и sid (адрес и без того длинный, а CDN иногда
        режет длинные URL).
        """
        host = urllib.parse.urlsplit(target).netloc.lower()
        if not host:
            return {}
        for session in STATE.sessions.all():
            stream = session.stream
            if stream is None or not stream.headers:
                continue
            candidates = [q.url for q in stream.qualities]
            if stream.audio_url:
                candidates.append(stream.audio_url)
            if any(urllib.parse.urlsplit(url).netloc.lower() == host for url in candidates):
                return dict(stream.headers)
        return {}

    def _from_cache(self, cache_key: str, wanted: str) -> bool:
        """Отдать кусок из памяти, если он там есть. True — уже отдали.

        Вынесено из `_bridge` отдельным методом: у моста и без того шесть
        развилок, и каждая новая делает его нечитаемым.
        """
        cached = STATE.segments.get(cache_key)
        if cached is None:
            return False
        body, ctype, extra_raw = cached
        extra = {"Content-Range": extra_raw} if extra_raw else None
        self._send(
            HTTP_PARTIAL if wanted else 200,
            body,
            ctype,
            cache="no-store",
            extra=extra,
        )
        return True

    def _bridge(  # noqa: PLR0911 — плоский список развилок читается лучше
        self, query: dict[str, list[str]]
    ) -> None:
        """Универсальный мост: плейлист или сегмент источника.

        Заголовки берутся из ДВУХ мест: `_referer_headers` знает про домены
        (Kodik, Rutube, VK), а `Stream.headers` — про конкретную подпись.
        Второе важнее и перекрывает первое: у okcdn подпись привязана к
        отпечатку браузера (`srcAg=GECKO`), и запрос с Chrome-UA получает
        400, даже если адрес правильный. Проверено живьём: без этого видео
        anidub.org не играло вовсе.
        """
        target = (query.get("url") or [""])[0]
        if not target:
            self._error_playlist("нужен параметр url", 400)
            return None
        headers = _referer_headers(target)
        headers.update(self._stream_headers(target))
        # Перемотка. Браузер просит кусок файла заголовком `Range`, и этот
        # кусок надо попросить у источника — иначе он приходит целиком, мы
        # режем его на 8 МБ, и плеер получает битый поток: «Видео не
        # воспроизводится, ошибка декодирования» при ±10 секундах.
        wanted = str(self.headers.get("Range") or "").strip()
        # Повторный запрос того же куска — из памяти. Первый раз мог
        # прийти медленнее, чем ждёт hls.js (`fragLoadTimeOut`), и плеер
        # повторит адрес слово в слово; второй раз обязан быть мгновенным.
        cache_key = f"{target}|{wanted}"
        if self._from_cache(cache_key, wanted):
            return None
        # СЕГМЕНТ — отдаём потоком, ещё до того как он скачался целиком.
        # Замерено 26.11.2026 на anidub.com: сегмент 3 МБ приходит 22 с.
        # Пока он копился в память, hls.js сдавался и просил заново, а
        # повтор лез в тот же медленный канал — отсюда `fragLoadError
        # HTTP 502` и «серия не запускается». Плейлисты по-прежнему идут
        # через fetch: их надо переписать.
        if not _looks_like_playlist(target):
            return self._send_stream(target, headers, "video/mp2t", cache_key)
        try:
            # file:// — склейка ffmpeg (видео+звук): отдаём плейлист и сегменты
            # с диска тем же путём, что и сетевые. Отдельной ветки не нужно:
            # fetch умеет и локальные файлы, а rewrite — абсолютные пути.
            response = fetch(
                target,
                headers,
                max_bytes=8 * 1024 * 1024,
                range_header=wanted,
            )
        except FetchError as exc:
            return self._error_playlist(exc.reason, exc.status)

        if not hls_bridge.is_playlist(response.body):
            # сегмент или кусок файла: отдаём как есть, без «переписывания»
            ctype = response.content_type or "video/mp2t"
            if response.status == HTTP_PARTIAL:
                part_headers: dict[str, str] = {}
                content_range = response.headers.get("content-range")
                if content_range:
                    part_headers["Content-Range"] = content_range
                # Куски прямого файла не кэшируем: их просят под перемотку
                # и адреса всегда разные.
                return self._send(HTTP_PARTIAL, response.body, ctype, extra=part_headers)
            # Сегмент — кладём в память: следующий запрос будет мгновенным.
            STATE.segments.put(cache_key, response.body, ctype)
            return self._send(200, response.body, ctype, cache="no-store")

        text = response.text()
        rewritten = hls_bridge.rewrite(text, response.final_url, "/hls")
        return self._playlist(rewritten.encode())

    def _diag_ffmpeg(self, _query: dict[str, list[str]]) -> None:
        """Есть ли ffmpeg ВНУТРИ собранной программы и работает ли он.

        Зачем маршрут: у заказчика «Замер не удался», а в исходниках ffmpeg
        находится. Разница только в собранном .exe (PyInstaller распаковывает
        пакет во временную папку), и проверить это можно лишь спросив у самой
        программы — что она видит.
        """
        from ..core import ffmpeg_bin  # noqa: PLC0415

        path = ffmpeg_bin.find_ffmpeg()
        info: dict[str, Any] = {
            "found": bool(path),
            "path": path or "",
            "version": ffmpeg_bin.version(),
            "frozen": bool(getattr(sys, "frozen", False)),
            "platform": sys.platform,
        }
        if path:
            info["exists"] = Path(path).is_file()
            try:
                # Путь наш, из своей же сборки — оболочку не задействуем.
                check = subprocess.run(
                    [path, "-version"], capture_output=True, text=True, timeout=20, check=False
                )
                info["runs"] = check.returncode == 0
                first = (check.stdout or check.stderr or "").splitlines()
                info["banner"] = first[0][:120] if first else ""
            except Exception as exc:  # показываем как есть
                info["runs"] = False
                info["error"] = f"{type(exc).__name__}: {exc}"
        self._json(200, info)

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
                ffmpeg_exe = _ffmpeg_exe()
                if ffmpeg_exe is None:
                    # Не «замер не удался», а конкретно: нет инструмента, и
                    # вот что с этим делать. Раньше обе причины сливались в
                    # одно «Замер не удался», и человек не знал, чинить ему
                    # поток, сеть или что-то ещё.
                    return self._json(
                        200,
                        {
                            "gainDb": None,
                            "note": "нужен ffmpeg",
                            "fix": (
                                "Положите ffmpeg.exe рядом с программой (в папку bin) "
                                "или установите: winget install ffmpeg"
                            ),
                        },
                    )
                value = loudness.measure(url, ffmpeg_exe, _measure_headers(session, url))
                if value is None:
                    return self._json(
                        200,
                        {
                            "gainDb": None,
                            "note": "замер не удался",
                            "fix": (
                                "Источник отдал поток, который ffmpeg не смог прочитать. "
                                "Нормализация необязательна — звук играет и без неё"
                            ),
                        },
                    )
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


def _measure_headers(session: Any, url: str) -> dict[str, str]:
    """Заголовки для ffmpeg: сначала то, что просит источник, потом общее.

    Источник знает свой CDN лучше, чем сервер: у anidub рабочий UA — именно
    Firefox, на Chrome-CDN отвечает 403. Если брать только общие заголовки
    по адресу, этот UA теряется, и замер громкости молча не срабатывает
    (человек видит «замер не удался» без причины).
    """
    headers = dict(getattr(session.stream, "headers", None) or {})
    for name, value in _referer_headers(url).items():
        headers.setdefault(name, value)
    return headers


def _ffmpeg_exe() -> str | None:
    """Путь к ffmpeg. Без него замер громкости не запустится.

    `measure()` первым аргументом после адреса ждёт именно исполняемый
    файл, а не заголовки — раньше сюда приезжал словарь, и ffmpeg падал
    ещё до запуска (`expected str, bytes or os.PathLike object, not dict`).
    """
    from ..core.ffmpeg_bin import find_ffmpeg  # noqa: PLC0415

    return find_ffmpeg()


def _referer_headers(url: str) -> dict[str, str]:
    """Referer под источник. Kodik подписывает запросы под домен-партнёр."""
    low = url.lower()
    if "solodcdn" in low or "kodik" in low:
        return {"Referer": "https://kodikplayer.com/", "Origin": "https://kodikplayer.com"}
    if "rutube" in low or "rtbcdn" in low:
        return {"Referer": "https://rutube.ru/"}
    if "okcdn" in low or "vkvd" in low or "mycdn" in low:
        return {"Referer": "https://vk.com/"}
    if "anivids" in low or "ladonyvesna" in low:
        # Балансер anidub отдаёт 403 без Referer сайта.
        return {"Referer": "https://anidub.com/", "Accept-Language": "ru-RU,ru;q=0.9"}
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
