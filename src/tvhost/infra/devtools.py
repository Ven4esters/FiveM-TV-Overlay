"""
Поиск потоков «как в браузере»: запускаем установленный Chrome/Edge в фоне
и перехватываем сетевые запросы (то же, что вкладка F12 → Сеть → Медиа).

Зачем это нужно:
    Многие плееры (например, на сериальных сайтах) не держат ссылку на поток
    в тексте страницы. Они склеивают её скриптом уже после выбора озвучки:
        https://cdn.site/s/<токен>/<время>/<id-озвучки>/index.m3u8
    Такую ссылку можно увидеть только в сетевых запросах. Этот модуль их ловит.

Почему без сторонних библиотек:
    Chrome общается по протоколу DevTools через WebSocket. Здесь он реализован
    вручную на стандартной библиотеке (socket + hashlib), поэтому ничего
    доустанавливать не нужно — используется ваш уже установленный браузер.

Поддерживается: Chrome, Edge, Chromium, Opera, Brave (Windows/Linux/macOS).
Не требуется: Playwright, selenium, pip-пакеты.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlsplit

# --------------------------------------------------------------------------
# 1. Поиск установленного браузера
# --------------------------------------------------------------------------

_WIN_PATHS = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%LocalAppData%\Programs\Opera\opera.exe",
    r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
]

_POSIX_PATHS = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
    "/usr/bin/brave-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def _expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def find_browser() -> str:
    """Путь к Chrome/Edge/Chromium или пустая строка."""
    env = os.environ.get("GTA_BROWSER", "").strip()
    if env and os.path.exists(_expand(env)):
        return _expand(env)

    for p in _WIN_PATHS + _POSIX_PATHS:
        p = _expand(p)
        if os.path.exists(p):
            return p

    for name in (
        "google-chrome",
        "chrome",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "msedge",
        "brave-browser",
    ):
        found = shutil.which(name)
        if found:
            return found

    # Chromium, который иногда лежит внутри пакета playwright
    for root in (
        os.path.expanduser("~/.cache/ms-playwright"),
        os.path.expanduser("~/AppData/Local/ms-playwright"),
    ):
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for exe in ("chrome", "chrome.exe", "headless_shell", "headless_shell.exe"):
                if exe in files:
                    return os.path.join(dirpath, exe)
    return ""


def is_available() -> bool:
    return bool(find_browser())


# --------------------------------------------------------------------------
# 2. Минимальный WebSocket-клиент (стандартная библиотека)
# --------------------------------------------------------------------------


class _WS:
    """Клиент WebSocket только для протокола DevTools (текстовые кадры)."""

    def __init__(self, ws_url: str, timeout: float = 20.0):
        u = urlsplit(ws_url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        path = u.path + (("?" + u.query) if u.query else "")

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        self._buf = b""
        # читаем ответ до пустой строки
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("браузер закрыл соединение при подключении")
            self._buf += chunk
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError(
                "DevTools не принял подключение: "
                + head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            )
        self._id = 0
        self._queue: list[dict] = []
        self._events: list[dict] = []  # всё, что пришло от браузера
        self._lock = threading.Lock()
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        """Единственное место, где читается сокет: складываем всё в очередь."""
        while self._alive:
            try:
                obj = self._recv_raw(timeout=0.5)
            except TimeoutError:
                continue
            except (ConnectionError, OSError):
                break
            if not isinstance(obj, dict):
                continue
            with self._lock:
                self._queue.append(obj)
                self._events.append(obj)

    def drain_events(self) -> list[dict]:
        """Забрать накопленные события (без ответов на команды)."""
        with self._lock:
            ev = [o for o in self._events if "method" in o]
            self._events = [o for o in self._events if "method" not in o]
        return ev

    # --- отправка ---
    def send(self, payload: str) -> None:
        data = payload.encode("utf-8")
        header = bytearray([0x81])  # FIN + текстовый кадр
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def call(
        self, method: str, params: dict | None = None, session_id: str = "", timeout: float = 20.0
    ) -> dict:
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self.send(json.dumps(msg))
        want = self._id
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                for i, obj in enumerate(self._queue):
                    if obj.get("id") == want:
                        self._queue.pop(i)
                        if "error" in obj:
                            raise RuntimeError(
                                f"{method}: {obj['error'].get('message', obj['error'])}"
                            )
                        return obj
            time.sleep(0.02)
        raise TimeoutError(f"нет ответа на {method}")

    def send_only(self, method: str, params: dict | None = None) -> int:
        """
        Отправить команду и НЕ ждать ответа.

        Нужно для Page.navigate: Chrome выполняет переход, но ответ на команду
        иногда не присылает (известная особенность headless-режима).
        """
        self._id += 1
        self.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        return self._id

    # --- приём ---
    def _stash(self, obj: dict) -> None:
        self._queue.append(obj)

    def _read_exact(self, n: int, timeout: float) -> bytes:
        end = time.time() + timeout
        while len(self._buf) < n:
            left = end - time.time()
            if left <= 0:
                raise TimeoutError("таймаут ожидания данных от браузера")
            self.sock.settimeout(left)
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("браузер закрыл соединение")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self, timeout: float) -> tuple[int, bytes, bool]:
        """Вернуть (opcode, data, fin) — fin=False значит «будут ещё части»."""
        b1, b2 = self._read_exact(2, timeout)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._read_exact(2, timeout))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._read_exact(8, timeout))[0]
        mask = self._read_exact(4, timeout) if masked else b""
        data = self._read_exact(ln, timeout) if ln else b""
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return opcode, data, fin

    def recv(self, timeout: float = 20.0) -> dict | None:
        """Забрать из очереди что угодно (ответ или событие)."""
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if self._queue:
                    return self._queue.pop(0)
            time.sleep(0.02)
        return None

    def _recv_raw(self, timeout: float = 20.0) -> dict | None:
        chunks: list[bytes] = []
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError("нет кадра от браузера")
            opcode, data, fin = self._read_frame(max(0.1, left))
            if opcode in (0x1, 0x0):  # текст или продолжение текста
                chunks.append(data)
                if fin:
                    break  # сообщение собрано целиком
                continue
            if opcode == 0x8:  # закрытие
                raise ConnectionError("браузер закрыл соединение")
            if opcode == 0x9:  # ping → pong
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            # бинарные кадры DevTools не использует
        try:
            return json.loads(b"".join(chunks).decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return None

    def close(self) -> None:
        self._alive = False
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# 3. Перехват потоков
# --------------------------------------------------------------------------

_STREAM_EXT = re.compile(r"\.(m3u8|mpd)(\?|$)", re.I)
_FILE_EXT = re.compile(r"\.(mp4|webm|m4v)(\?|$)", re.I)

# Названия озвучек, по которым можно кликнуть, чтобы плеер собрал ссылку
_STUDIOS = [
    "AMedia",
    "LostFilm",
    "HDrezka",
    "Кубик в Кубе",
    "Jaskier",
    "AniLibria",
    "AniDUB",
    "AnimeVost",
    "СВ-Дубль",
    "Пифагор",
    "Студийная банда",
    "NewStudio",
    "AlexFilm",
    "BaibaKo",
    "ViruseProject",
    "Субтитры",
]


def _http_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


class BrowserSniffer:
    """
    Запустить браузер, открыть страницу и собрать ссылки на медиа.

    Подключение идёт напрямую к вкладке через её адрес отладки
    (список вкладок отдаёт /json/list) — это надёжнее, чем Target.attach,
    и не требует ни одной сторонней библиотеки.

    sniffer = BrowserSniffer()
    sniffer.open(page_url)
    streams = sniffer.collect(seconds=12, click_voice="AMedia")
    sniffer.close()
    """

    def __init__(self, timeout: float = 25.0):
        self.exe = find_browser()
        if not self.exe:
            raise RuntimeError(
                "не найден Chrome/Edge. Установите Chrome или укажите путь в переменной GTA_BROWSER"
            )
        self.timeout = timeout
        self.port = 0
        self.proc: subprocess.Popen | None = None
        self.ws: _WS | None = None
        self.profile = ""
        self.requests: list[str] = []
        self.notes: list[str] = []

    # --- запуск ---
    def start(self, url: str = "about:blank") -> None:
        self.profile = tempfile.mkdtemp(prefix="gta-sniff-")
        args = [
            self.exe,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--mute-audio",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=CalculateNativeWinOcclusion",
            "--disable-dev-shm-usage",
            "--disable-ipc-flooding-protection",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,800",
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            f"--user-data-dir={self.profile}",
            "--remote-debugging-port=0",
            url,
        ]
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation,
        )

        port_file = os.path.join(self.profile, "DevToolsActivePort")
        end = time.time() + self.timeout
        while time.time() < end:
            if os.path.exists(port_file):
                try:
                    with open(port_file, encoding="utf-8") as f:
                        parts = f.read().split()
                    if parts:
                        self.port = int(parts[0])
                        break
                except (OSError, ValueError):
                    pass
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(
                    "браузер завершился при запуске (возможно, он уже открыт с отладкой)"
                )
            time.sleep(0.15)
        if not self.port:
            raise RuntimeError("браузер не открыл порт отладки")
        self._connect_page()

    def _pages(self) -> list[dict]:
        try:
            tabs = _http_json(f"http://127.0.0.1:{self.port}/json/list")
        except Exception:
            return []
        return [t for t in tabs if t.get("type") == "page"]

    def _connect_page(self) -> None:
        end = time.time() + self.timeout
        while time.time() < end:
            for t in self._pages():
                url = t.get("webSocketDebuggerUrl") or ""
                if not url:
                    continue
                ws = None
                try:
                    ws = _WS(url, timeout=self.timeout)
                    ws.call("Page.enable", timeout=10)
                    ws.call("Network.enable", timeout=10)
                    self.ws = ws
                    # вкладка должна быть «живой», иначе переходы не выполняются
                    for cmd in ("Page.bringToFront", "Page.setWebLifecycleState"):
                        try:
                            params = {"state": "active"} if cmd.endswith("State") else {}
                            ws.call(cmd, params, timeout=4)
                        except Exception:
                            pass
                    return
                except Exception:
                    # каждое подключение — отдельный, свежий счётчик команд;
                    # повторное использование сокета путало ответы (id 1 дважды)
                    if ws is not None:
                        ws.close()
            time.sleep(0.2)
        raise RuntimeError("не удалось подключиться к вкладке браузера")

    def _wait_loaded(self, seconds: float = 20.0) -> None:
        """Дождаться загрузки, попутно вычитывая события (иначе Chrome замирает)."""
        end = time.time() + seconds
        while time.time() < end:
            self._drain(0.4)
            try:
                v = self._ev("document.readyState", timeout=3)
                if v in ("interactive", "complete"):
                    return
            except Exception:
                pass

    # --- страница ---
    def open(self, url: str) -> None:
        if self.ws is None:
            self.start(url)  # открываем нужную страницу сразу при старте
            self._wait_loaded()
            return
        # Переход: команду отправляем и тут же вычитываем события. Если просто
        # ждать ответ — Chrome «замирает» на headless-режиме.
        try:
            self.ws.send_only("Page.navigate", {"url": url})
        except OSError:
            pass
        self._drain(1.5)
        end = time.time() + 20
        while time.time() < end:
            try:
                href = self._ev("location.href", timeout=4) or ""
            except Exception:
                href = ""
            if href.startswith("http") and href.split("#")[0] != "about:blank":
                self._wait_loaded(15)
                return
            time.sleep(0.3)
        # запасной путь — переход через JS
        try:
            self._ev(f"location.href = {json.dumps(url)}", timeout=5)
        except Exception:
            pass
        self._wait_loaded(15)

    def _ev(self, expression: str, timeout: float = 10.0):
        if self.ws is None:
            raise RuntimeError("нет соединения с браузером: сначала подключитесь")
        r = self.ws.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        return r.get("result", {}).get("result", {}).get("value")

    def wait_ready(self, seconds: float = 15.0) -> None:
        self._wait_loaded(seconds)

    def _drain(self, seconds: float) -> None:
        """
        Подождать указанное время и разобрать накопленные события.

        Сокет читает фоновый поток, поэтому здесь только просмотр журнала —
        ни один сетевой запрос не теряется.
        """
        time.sleep(max(0.0, seconds))
        if self.ws is None:
            return
        for obj in self.ws.drain_events():
            method = obj.get("method")
            params = obj.get("params") or {}
            if method in ("Network.requestWillBeSent", "Network.responseReceived"):
                url = (params.get("request") or params.get("response") or {}).get("url", "")
                if url and url not in self.requests:
                    self.requests.append(url)
                    if _STREAM_EXT.search(url):
                        self.notes.append("поток: " + url[-60:])
            elif method == "Page.javascriptDialogOpening":
                # всплывающее окно сайта может блокировать плеер
                try:
                    if self.ws is not None:
                        self.ws.call("Page.handleJavaScriptDialog", {"accept": True}, timeout=3)
                except Exception:
                    pass

    def _click_voice(self, voice: str = "") -> bool:
        """
        Нажать на кнопку озвучки, чтобы плеер собрал ссылку.

        Ищем по названиям студий (AMedia, LostFilm, AniLibria…) — так устроены
        сериальные базы. Сначала пробуем заданное название, затем остальные.
        """
        names = [voice] if voice else []
        names += [s for s in _STUDIOS if s.lower() != (voice or "").lower()]
        for name in names:
            js = """
            (() => {
              const want = %s.toLowerCase();
              const sel = 'a,button,div,span,li,label,option,[role=button],[data-translation]';
              const els = Array.from(document.querySelectorAll(sel));
              // Берём только «листья»: у обёртки <div> текст такой же, как у
              // кнопки внутри, но клик по обёртке ничего не делает.
              const leaves = els.filter(el => el.childElementCount === 0);
              const pool = leaves.length ? leaves : els;
              for (const el of pool) {
                const t = (el.textContent || '').trim().toLowerCase();
                if (!t || t.length > 60) continue;
                if (!t.includes(want)) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) continue;
                el.click();
                return (el.tagName + ':' + t.slice(0, 40));
              }
              return '';
            })()
            """ % json.dumps(name)
            try:
                hit = self._ev(js, timeout=8)
            except Exception:
                hit = ""
            if hit:
                self.notes.append(f"нажали озвучку: {hit}")
                return True
        return False

    def _frames(self) -> list[str]:
        """Адреса всех iframe на странице — плеер часто внутри."""
        try:
            return (
                self._ev(
                    "Array.from(document.querySelectorAll('iframe'))"
                    ".map(f => f.src).filter(Boolean)"
                )
                or []
            )
        except Exception:
            return []

    def _media_urls(self) -> list[str]:
        return [u for u in self.requests if _STREAM_EXT.search(u)]

    def collect(self, seconds: float = 12.0, voice: str = "") -> list[dict]:
        """
        Собрать найденные потоки. Возвращает список словарей:
            {"url", "kind": "hls"|"file", "via": "browser", "where"}
        """
        self.wait_ready(15)
        self._drain(3.0)
        self.notes.append(f"после загрузки запросов: {len(self.requests)}")

        # поток не появился сам — жмём озвучку и ждём ещё
        if not self._media_urls():
            if self._click_voice(voice):
                self._drain(min(8.0, seconds))
                self.notes.append(f"после клика запросов: {len(self.requests)}")

        # всё ещё пусто — заглядываем внутрь iframe
        if not self._media_urls():
            for src in self._frames()[:3]:
                try:
                    if self.ws is not None:
                        self.ws.send_only("Page.navigate", {"url": src})
                    self._drain(1.5)
                    self._wait_loaded(10)
                    self._drain(min(6.0, seconds))
                except Exception:
                    continue
                if self._media_urls():
                    break

        self._drain(max(2.0, min(6.0, seconds / 2)))
        self.notes.append(f"итого запросов: {len(self.requests)}")
        return self._result()

    def _result(self) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()

        def add(u: str, kind: str) -> None:
            if u in seen:
                return
            seen.add(u)
            out.append({"url": u, "kind": kind, "via": "browser", "where": "перехвачено браузером"})

        for u in self.requests:
            if u.startswith(("data:", "blob:", "chrome-extension:")):
                continue
            if _STREAM_EXT.search(u):
                add(u, "hls")
        if not out:
            # иногда плеер сразу тянет mp4
            for u in self.requests:
                if _FILE_EXT.search(u) and "thumb" not in u.lower():
                    add(u, "file")
        return out

    def close(self) -> None:
        try:
            if self.ws:
                try:
                    self.ws.call("Browser.close", timeout=3)
                except Exception:
                    pass
                self.ws.close()
        except Exception:
            pass
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass
        self._cleanup_profile()

    def _cleanup_profile(self) -> None:
        """
        Удалить временный профиль. На Windows Chrome отпускает файлы с задержкой,
        поэтому пробуем несколько раз — иначе профили копятся в %TEMP%.
        """
        if not self.profile:
            return
        for _ in range(6):
            shutil.rmtree(self.profile, ignore_errors=True)
            if not os.path.exists(self.profile):
                return
            time.sleep(0.4)
        # последняя попытка: удалить при следующем запуске системы не мешает,
        # но и «мусор» в папке уже не критичен — объём профиля ~1 МБ
        shutil.rmtree(self.profile, ignore_errors=True)


def launch_browser_with_debug(port: int = 9222, profile_dir: str = "") -> subprocess.Popen:
    """
    Запустить Chrome/Edge с отладочным портом — то же, что делает
    «1. Браузер для поиска потоков.bat», но из кода (для проверок).
    """
    exe = find_browser()
    if not exe:
        raise RuntimeError("не найден Chrome/Edge")
    prof = profile_dir or tempfile.mkdtemp(prefix="gta-catch-")
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.Popen(
        [
            exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={prof}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation,
    )


def connect_to_browser(
    debug_url: str = "http://127.0.0.1:9222", wait_seconds: float = 120.0
) -> list[dict]:
    """
    Поймать потоки из УЖЕ ОТКРЫТОГО браузера пользователя.

    Схема (как в расширениях hls-stream-detector, но без установки):
      1. Пользователь закрывает Chrome/Edge и запускает его ярлыком
         «1. Браузер для поиска потоков.bat» (в нём включён отладочный порт).
      2. В своём обычном браузере включает видео в нужной озвучке —
         плеер строит ту самую подписанную ссылку .m3u8.
      3. Приложение подключается к этому браузеру и забирает найденные
         адреса — включая те, что видны только внутри вкладки.

    Ничего не устанавливается и не перехватывается постоянно: подключение
    живёт только на время поиска.

    Возвращает список потоков [{url, kind}] или бросает RuntimeError.
    """
    base = debug_url.rstrip("/")
    try:
        tabs = _http_json(base + "/json/list")
    except Exception:
        raise RuntimeError(
            "браузер с отладкой не найден. Закройте Chrome/Edge и запустите "
            "«1. Браузер для поиска потоков.bat» из папки программы"
        )

    pages = [t for t in tabs if t.get("type") == "page" and (t.get("url") or "").startswith("http")]
    if not pages:
        raise RuntimeError(
            "в браузере нет открытой страницы с видео. Откройте страницу, "
            "включите нужную озвучку и нажмите «Из моего браузера» ещё раз"
        )

    found: list[dict] = []
    seen: set[str] = set()

    # Cookie всего браузера: многие CDN проверяют их вместе с подписью URL.
    all_cookies = ""
    try:
        tab0 = next(t for t in pages if t.get("webSocketDebuggerUrl"))
        ws0 = _WS(tab0["webSocketDebuggerUrl"], timeout=10)
        try:
            ws0.call("Network.enable", timeout=8)
            res = ws0.call("Network.getAllCookies", timeout=8)
            jar = (res.get("result") or {}).get("cookies") or []
            all_cookies = "; ".join(
                f"{c.get('name')}={c.get('value')}" for c in jar if c.get("name") and c.get("value")
            )
        finally:
            ws0.close()
    except Exception:
        all_cookies = ""

    # берём вкладки с видео в первую очередь
    pages.sort(
        key=lambda t: (
            0
            if any(
                w in (t.get("url") or "").lower()
                for w in ("video", "season", "seriy", "episode", "film", "watch")
            )
            else 1
        )
    )

    end = time.time() + wait_seconds
    for tab in pages[:4]:
        url = tab.get("webSocketDebuggerUrl") or ""
        if not url:
            continue
        ws = None
        try:
            ws = _WS(url, timeout=10)
            ws.call("Network.enable", timeout=8)
            # слушаем уже идущие запросы: если видео играет — поток придёт сразу
            while time.time() < end and len(found) < 30:
                try:
                    obj = ws.recv(timeout=1.5)
                except TimeoutError:
                    break
                except OSError:
                    break
                if not isinstance(obj, dict):
                    continue
                method = obj.get("method")
                if method in ("Network.requestWillBeSent", "Network.responseReceived"):
                    prm = obj.get("params", {})
                    req = prm.get("request") or prm.get("response") or {}
                    u = req.get("url", "")
                    if not u or u in seen:
                        continue
                    seen.add(u)
                    if _STREAM_EXT.search(u) or (_FILE_EXT.search(u) and "thumb" not in u.lower()):
                        # Контекст запроса — это и есть «сессия»:
                        # по нему CDN узнаёт, что ссылку выдал именно он.
                        hd = req.get("headers") or {}
                        hd = {str(k).lower(): str(v) for k, v in hd.items()}
                        found.append(
                            {
                                "url": u,
                                "kind": "hls" if _STREAM_EXT.search(u) else "file",
                                "via": "browser",
                                "where": "из вашего браузера",
                                "referer": hd.get("referer", ""),
                                "origin": hd.get("origin", ""),
                                "user_agent": hd.get("user-agent", "")
                                or (
                                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/120.0 Safari/537.36"
                                ),
                                "cookies": hd.get("cookie", "") or all_cookies,
                                "page": prm.get("documentURL", "") or tab.get("url", ""),
                            }
                        )
        except Exception:
            continue
        finally:
            if ws is not None:
                ws.close()

    return found


def sniff(page_url: str, seconds: float = 12.0, voice: str = "") -> list[dict]:
    """
    Открыть страницу в настоящем браузере и вернуть найденные потоки.

    Бросает RuntimeError, если браузера нет.
    """
    sn = BrowserSniffer()
    try:
        sn.open(page_url)
        return sn.collect(seconds=seconds, voice=voice)
    finally:
        sn.close()


if __name__ == "__main__":  # ручная проверка: python devtools.py <страница>
    target = sys.argv[1] if len(sys.argv) > 1 else "https://test-streams.mux.dev/"
    print("браузер:", find_browser() or "не найден")
    for st in sniff(target, seconds=8):
        print("  ", st["kind"], st["url"])
