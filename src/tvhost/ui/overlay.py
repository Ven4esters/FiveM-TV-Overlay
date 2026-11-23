"""
overlay.py — оверлей-пульт для GTA 5 (PySide6).

Возможности:
- Всегда поверх игры, не забирает фокус, прозрачность и компактный режим.
- Вставьте обычную ссылку (YouTube, сайт) → получите прямую для телевизора.
- История ссылок с копированием в один клик и запуском в браузере.
- Управление встроенным видео-прокси (proxy.py) прямо из окна: старт/стоп, статус.
- Перетаскивание окна, изменение размера, горячие клавиши.

Запуск:
    python overlay.py            (или start.bat в Windows)
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..infra import console_io  # noqa: F401 — UTF-8/эмодзи безопасно на Windows
from ..infra import diagnostics as diag
from ..infra.cache import CACHE_DIR as _CACHE_DIR  # noqa: F401
from . import icons as ui_icons

# Корень проекта: ui/ → tvhost/ → src/ → корень (для style.qss и icons/)
PROJECT_ROOT = Path(__file__).resolve().parents[3]
BASE_DIR = PROJECT_ROOT
HISTORY_PATH = BASE_DIR / "videos" / "overlay_history.json"
HISTORY_MAX = 60
DEFAULT_PROXY_PORT = 9000


# Если style.qss потерялся при копировании, интерфейс не должен
# превращаться в «стандартный Windows». Это краткая встроенная копия стилей.
URL_HINT_DEFAULT = "YouTube, VK, Rutube, Twitch, Kodik, сайты с видео, .m3u8 / .mp4"

FALLBACK_QSS = """
QWidget {
    background-color: #1E1E1E; color: #E0E0E0;
    font-family: "Segoe UI", "Roboto", "Inter", "Noto Sans", sans-serif;
    font-size: 14px;
}
QFrame#panel { background-color: #1E1E1E; border: 1px solid #333333; border-radius: 12px; }
QFrame#headerBar { background-color: #252526; border-top-left-radius: 12px;
                   border-top-right-radius: 12px; }
QFrame#card { background-color: #252526; border: 1px solid #333333; border-radius: 8px; }
QLabel#title { color: #F0F0F0; font-size: 16px; font-weight: 600; }
QLabel#sectionTitle { color: #E0E0E0; font-size: 14px; font-weight: 600; }
QLabel#caption, QLabel#hint { color: #8A8A8A; font-size: 12px; }
QLabel#mono { color: #C8C8C8; font-size: 13px; }
QLabel#hint[state="ok"] { color: #4ADE80; }
QLabel#hint[state="warn"] { color: #FBBF24; }
QLabel#hint[state="error"] { color: #F87171; }
QLineEdit, QComboBox {
    background-color: #2D2D30; color: #E0E0E0; border: 1px solid #3C3C3C;
    border-radius: 6px; padding: 8px 12px;
}
QLineEdit:focus, QComboBox:focus { border: 1px solid #3B82F6; }
QPushButton#primary {
    background-color: #3B82F6; color: #FFFFFF; border: 1px solid #3B82F6;
    border-radius: 6px; padding: 8px 16px; font-weight: 600;
}
QPushButton#primary:hover { background-color: #4B8DF8; }
QPushButton#primary:pressed { background-color: #2E6FD9; }
QPushButton#secondary {
    background-color: transparent; color: #C8D6F0; border: 1px solid #3F4A63;
    border-radius: 6px; padding: 8px 16px;
}
QPushButton#secondary:hover { background-color: #262B36; border-color: #55617E; }
QPushButton#ghost {
    background-color: transparent; color: #A0A0A0; border: 1px solid transparent;
    border-radius: 6px; padding: 4px 8px;
}
QPushButton#ghost:hover { background-color: #2D2D30; color: #E0E0E0; }
QPushButton#danger {
    background-color: transparent; color: #A0A0A0; border: 1px solid transparent;
    border-radius: 6px; padding: 4px 8px;
}
QPushButton#danger:hover { background-color: #C0392B; color: #FFFFFF; }
QPushButton#pad {
    background-color: transparent; color: #E0E0E0; border: 1px solid transparent;
    border-radius: 8px; font-size: 18px; min-width: 40px; min-height: 40px;
}
QPushButton#pad:hover { background-color: #2D2D30; border-color: #3C3C3C; }
QPushButton#pad[active="true"] { background-color: #1E3A5F; border-color: #3B82F6; }
QPushButton#inlineCopy {
    background-color: transparent; color: #A0A0A0; border: none; padding: 0 12px;
}
QPushButton#inlineCopy:hover { background-color: #3A3A3D; color: #FFFFFF; }
QListWidget { background-color: #252526; color: #D0D0D0; border: 1px solid #333333;
              border-radius: 6px; padding: 4px; }
QListWidget::item { padding: 8px; }
QListWidget::item:selected { background-color: #1E3A5F; color: #FFFFFF; }
QCheckBox { color: #C8C8C8; font-size: 13px; }
"""


def load_history() -> list[dict]:
    try:
        raw = json.loads(HISTORY_PATH.read_text("utf-8"))
        return [h for h in raw if isinstance(h, dict) and h.get("url")]
    except (OSError, json.JSONDecodeError):
        return []


def save_history(items: list[dict]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(items[:HISTORY_MAX], ensure_ascii=False, indent=1), "utf-8")


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def is_our_proxy(port: int, timeout: float = 1.5) -> bool:
    """Проверить, что на порту слушает именно наш proxy.py (а не чужой сервис).

    Наш прокси отвечает на /list валидным JSON с ключом 'items'.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/list", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
            return isinstance(data, dict) and "items" in data
    except (urllib.error.URLError, json.JSONDecodeError, OSError, TimeoutError):
        return False


class NoteWindow(QWidget):
    """Всплывающее окно-подсказка (ошибки, лог, диагностика)."""

    @staticmethod
    def _icon(name: str, size: int = 20) -> QIcon:
        """Иконка из папки icons/ — та же реализация, что и в панели."""
        return ui_icons.icon(name, size)

    """
    Отдельное окно для подсказок, ошибок и лога прокси.

    Зачем отдельное: длинный текст в панели оверлея не влезал
    (обрезался снизу) и растягивал главное окно. Здесь своё окно с
    прокруткой — текст любой длины читается целиком, а пульт остаётся
    компактным.

    Окно безрамочное, поверх игры, перетаскивается за шапку.
    """

    retry_requested = Signal()
    copy_taken = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._drag_pos = None
        self.setWindowTitle("Сообщение")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("noteWindow")
        self.setMinimumWidth(420)
        self.setMinimumHeight(240)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # шапка
        self.head = QFrame()
        self.head.setObjectName("headerBar")
        hl = QHBoxLayout(self.head)
        hl.setContentsMargins(16, 8, 8, 8)
        hl.setSpacing(8)
        self.lbl_icon = QLabel()
        self.lbl_icon.setObjectName("noteIcon")
        self.lbl_icon.setFixedSize(16, 16)
        hl.addWidget(self.lbl_icon)
        self.lbl_title = QLabel("Сообщение")
        self.lbl_title.setObjectName("headerTitle")
        hl.addWidget(self.lbl_title)
        hl.addStretch(1)

        self.btn_close = QPushButton()
        self.btn_close.setIcon(self._icon("close", 16))
        self.btn_close.setObjectName("ghost")
        self.btn_close.setFixedSize(32, 32)
        self.btn_close.setToolTip("Закрыть (Esc)")
        self.btn_close.clicked.connect(self.hide)
        hl.addWidget(self.btn_close)
        outer.addWidget(self.head)

        body = QWidget()
        body.setObjectName("content")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(16, 16, 16, 16)
        bl.setSpacing(8)

        # текст в прокручиваемой области: ничего не обрезается
        self.txt = QLabel("")
        self.txt.setObjectName("infoText")
        self.txt.setWordWrap(True)
        self.txt.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.txt.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        # имя scroll_area, а не scroll: scroll() — метод QWidget,
        # и атрибут с тем же именем путает и mypy, и читателя
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("noteScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setWidget(self.txt)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        bl.addWidget(self.scroll_area, 1)

        # кнопки
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.btn_copy = QPushButton("Копировать")
        self.btn_copy.setObjectName("secondary")
        self.btn_copy.setVisible(False)
        self.btn_copy.clicked.connect(self._copy)
        self.btn_retry = QPushButton("Повторить")
        self.btn_retry.setObjectName("secondary")
        self.btn_retry.setVisible(False)
        self.btn_retry.clicked.connect(self._retry)
        btn_ok = QPushButton("Закрыть")
        btn_ok.setObjectName("ghost")
        btn_ok.clicked.connect(self.hide)
        row.addWidget(self.btn_copy)
        row.addWidget(self.btn_retry)
        row.addWidget(btn_ok)
        bl.addLayout(row)
        outer.addWidget(body, 1)

        self._copy_text = ""

    # ---------- наполнение ----------
    def show_note(
        self,
        html_text: str,
        kind: str = "info",
        title: str = "",
        copy_text: str | None = None,
        retry: bool = False,
        anchor: QWidget | None = None,
    ) -> None:
        titles = {"info": "Подсказка", "warn": "Внимание", "error": "Ошибка", "success": "Готово"}
        self.lbl_title.setText(title or titles.get(kind, "Сообщение"))
        icons = {"info": "help", "warn": "help", "error": "close", "success": "play"}
        self.lbl_icon.setPixmap(self._pixmap(icons.get(kind, "help")))
        for w in (self.txt, self.head):
            if w.property("kind") != kind:
                w.setProperty("kind", kind)
                w.style().unpolish(w)
                w.style().polish(w)
        self.txt.setText(html_text)
        self._copy_text = copy_text or ""
        self.btn_copy.setVisible(bool(copy_text))
        self.btn_retry.setVisible(retry)
        self.adjustSize()
        self._place(anchor)
        self.show()
        self.raise_()
        self.activateWindow()

    @staticmethod
    def _pixmap(name: str):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons", f"{name}.svg")
        if not os.path.exists(path):
            return QPixmap()
        return QIcon(path).pixmap(16, 16)

    def _place(self, anchor: QWidget | None) -> None:
        """Поставить окно рядом с оверлеем, не вылезая за экран."""
        screen = (anchor or self).screen()
        area = screen.availableGeometry()
        w = max(self.sizeHint().width(), 480)
        h = min(max(self.sizeHint().height(), 240), 620)
        self.resize(w, h)
        if anchor is not None:
            g = anchor.frameGeometry()
            x = g.center().x() - w // 2
            y = g.bottom() + 8
            if y + h > area.bottom():
                y = max(area.top() + 20, g.top() - h - 8)
        else:
            x = area.center().x() - w // 2
            y = area.center().y() - h // 2
        x = max(area.left() + 8, min(x, area.right() - w - 8))
        y = max(area.top() + 8, min(y, area.bottom() - h - 8))
        self.move(x, y)

    # ---------- кнопки ----------
    def _copy(self) -> None:
        if self._copy_text:
            QApplication.clipboard().setText(self._copy_text)
            self.btn_copy.setText("Скопировано")
            QTimer.singleShot(1200, lambda: self.btn_copy.setText("Копировать"))
            self.copy_taken.emit(self._copy_text)

    def _retry(self) -> None:
        self.hide()
        self.retry_requested.emit()

    # ---------- перетаскивание ----------
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._drag_pos is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e) -> None:
        self._drag_pos = None

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(e)


class ProxyController:
    """Управляет процессом proxy.py. Пишет лог в файл, подбирает свободный порт."""

    LOG_PATH = BASE_DIR / "proxy.log"

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.port = DEFAULT_PROXY_PORT
        self.external = False  # на порту уже что-то слушает (не наш процесс)
        self.last_error = ""  # читаемая причина последнего сбоя

    # ---------- проверки ----------
    def _port_free(self, port: int) -> bool:
        return not port_in_use(port)

    def _find_free_port(self, preferred: int, attempts: int = 12) -> int | None:
        for candidate in range(preferred, preferred + attempts):
            if self._port_free(candidate):
                return candidate
        return None

    def is_alive(self) -> bool:
        if self.proc is not None:
            if self.proc.poll() is None:
                return port_in_use(self.port)
            return False
        if port_in_use(self.port):
            self.external = True
            return True
        return False

    # ---------- запуск ----------
    def start(self, port: int) -> bool:
        self.last_error = ""

        # 1) на порту уже работает НАШ прокси — переиспользуем его
        if port_in_use(port) and is_our_proxy(port):
            self.port = port
            self.external = True
            self.last_error = ""
            return True
        # 1б) порт занят посторонним сервисом — ищем свободный
        if port_in_use(port):
            free = self._find_free_port(port + 1)
            if free is None:
                self.last_error = (
                    f"порты {port}–{port + 11} заняты посторонними программами. "
                    "Освободите порт 9000 или перезагрузите ПК"
                )
                return False
            self.last_error = f"порт {port} занят другой программой — использую порт {free}"
            port = free

        # 2) если наш прежний процесс жив — не плодим второй
        if self.proc is not None and self.proc.poll() is None:
            return True

        # 3) пробуем запустить; если порт заняли между проверками — берём следующий
        tried: list[int] = []
        candidate: int | None = port
        for _ in range(12):
            if candidate is None:
                break
            tried.append(candidate)
            ok = self._spawn(candidate)
            if ok:
                self.port = candidate
                self.external = False
                self.last_error = ""
                return True
            # процесс умер — причина уже записана в last_error
            if "занят" not in self.last_error.lower():
                return False
            candidate = self._find_free_port(candidate + 1)
            if candidate is not None:
                self.last_error = ""
        return False

    def _spawn(self, port: int) -> bool:
        """Попытаться поднять proxy.py на конкретном порту."""
        # CREATE_NO_WINDOW существует только на Windows — берём через getattr,
        # иначе на Linux/macOS обращение к атрибуту упадёт ещё до запуска.
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            log = self.LOG_PATH.open("a", encoding="utf-8")
            log.write(
                f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"запуск прокси (tvhost.proxy) на порту {port} =====\n"
            )
            log.flush()
            # Принудительный UTF-8 для дочернего процесса: на русской Windows
            # по умолчанию cp1251, и любой print() с эмодзи роняет proxy.py.
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            env.setdefault("PYTHONUNBUFFERED", "1")
            # Запускаем прокси как модуль пакета (-m), а не файлом:
            # proxy.py в корне больше нет, он лежит в src/tvhost/.
            env["PYTHONPATH"] = os.pathsep.join(
                [str(BASE_DIR / "src"), env.get("PYTHONPATH", "")]
            ).strip(os.pathsep)
            self.proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "tvhost.proxy", "--port", str(port)],
                cwd=str(BASE_DIR),
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=creationflags,
            )
        except Exception as e:
            self.last_error = f"не удалось запустить proxy.py: {e}"
            return False

        # ждём до 8 секунд: либо порт открылся, либо процесс умер
        deadline = time.time() + 8
        while time.time() < deadline:
            if port_in_use(port):
                return True
            code = self.proc.poll()
            if code is not None:
                self.last_error = self._diagnose(code, port)
                return False
            time.sleep(0.1)

        self.last_error = (
            f"proxy.py не открыл порт {port} за 8 секунд. Смотрите лог: {self.LOG_PATH.name}"
        )
        return False

    def _diagnose(self, exit_code: int, port: int) -> str:
        """Понять причину падения по логу и коду выхода."""
        tail = ""
        try:
            tail = self.LOG_PATH.read_text("utf-8", errors="replace")[-2500:]
        except OSError:
            pass

        low = tail.lower()
        if "address already in use" in low or "10048" in low:
            return f"порт {port} занят другим процессом"
        if "no module named 'yt_dlp'" in low:
            return "не установлен yt-dlp — запустите start.bat (переустановит зависимости)"
        if "no module named" in low:
            missing = tail.split("No module named")[-1].split("'")[1:2]
            return f"не хватает Python-модуля {missing[0] if missing else ''} — запустите start.bat"
        if exit_code is not None and exit_code != 0:
            return (
                f"proxy.py завершился с кодом {exit_code}. "
                f"Последние строки лога: {tail.strip()[-300:]}"
            )
        return f"proxy.py не запустился. Лог: {self.LOG_PATH.name}"

    def log_tail(self, lines: int = 25) -> str:
        try:
            return "\n".join(
                self.LOG_PATH.read_text("utf-8", errors="replace").splitlines()[-lines:]
            )
        except OSError:
            return "(лог пока пуст)"

    # ---------- остановка ----------
    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self.proc.kill()
                except OSError:
                    pass
        self.proc = None


class Overlay(QWidget):
    status_signal = Signal(str)
    session_signal = Signal(str, str)  # (player_url, sid)
    state_signal = Signal(dict)  # состояние от страницы
    error_signal = Signal(str)  # текст ошибки
    sessions_signal = Signal(dict)  # список активных сессий
    diag_signal = Signal(dict)  # снимок диагностики
    pin_signal = Signal(dict)  # результат закрепления ссылки
    pins_signal = Signal(dict)  # список закреплённых ссылок

    def __init__(self) -> None:
        super().__init__()
        self._drag_pos: QPoint | None = None
        self._compact = False
        self.history = load_history()
        self.proxy = ProxyController()

        self.setWindowTitle("GTA Video Overlay")
        # ВАЖНО: без Qt.Tool — иначе окно не появляется в панели задач
        # и его невозможно вернуть, если оно скрылось.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
        )
        # НЕ используем WA_TranslucentBackground: из-за него под панелью
        # просвечивала игра, и текст сливался с фоном (визуальный шум).
        # Фон задаёт style.qss — плотный #1E1E1E.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(self._load_qss())
        self.resize(496, 760)
        self._build_ui()
        self._apply_history()

        # авто-старт прокси, если ещё не поднят
        threading.Thread(target=self._boot_proxy, daemon=True).start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_status)
        self.timer.start(2000)

        self.remote_timer = QTimer(self)
        self.remote_timer.timeout.connect(self._poll_remote)
        self.remote_timer.start(800)
        self.status_signal.connect(self._set_status)
        self.session_signal.connect(self._session_ready)
        self.state_signal.connect(self._apply_remote_state)
        self.error_signal.connect(self._remote_error)
        self.sessions_signal.connect(self._apply_sessions)
        self.diag_signal.connect(self._show_diag_window)
        self.pin_signal.connect(self._pin_done)
        self.pins_signal.connect(self._apply_pins)

        # Отдельное окно для подсказок/ошибок/лога — главное окно не растягивается
        self.note = NoteWindow()
        self.note.setStyleSheet(self._load_qss())
        self.note.retry_requested.connect(self._retry_last)

    # ---------- UI ----------
    # ---------- иконки ----------
    @staticmethod
    def _icon(name: str, size: int = 20) -> QIcon:
        """Иконка из папки icons/ (реализация — в ui/icons.py, общая для окон)."""
        return ui_icons.icon(name, size)

    # ---------- состояния через QSS ----------
    @staticmethod
    def _set_state(widget, state: str) -> None:
        """
        Поменять «состояние» виджета (idle / ok / warn / error) через QSS-свойство.

        Цвета живут в style.qss, а не в коде: правка стилей не требует
        пересборки логики, и все состояния описаны в одном месте.
        """
        if widget.property("state") == state:
            return
        widget.setProperty("state", state)
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()

    def _remote_state(self, state: str) -> None:
        self._set_state(self.lbl_remote, state)

    def _status_state(self, state: str) -> None:
        self._set_state(self.lbl_status, state)

    # ---------- загрузка стилей ----------
    @staticmethod
    def _load_qss() -> str:
        """Прочитать style.qss из папки проекта (иначе — встроенная копия)."""
        path = os.path.join(str(PROJECT_ROOT), "style.qss")
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return FALLBACK_QSS
        # QSS не умеет относительных путей к картинкам: подставляем
        # абсолютный путь к папке icons/ (иначе стрелка списков не видна)
        icons = str(PROJECT_ROOT / "icons").replace("\\", "/")
        return text.replace("@ICONS@", icons)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.panel = QFrame(self)
        self.panel.setObjectName("panel")
        outer.addWidget(self.panel)

        # Шапка должна идти от края до края, поэтому у самой панели
        # отступов нет: их задаёт внутренний контейнер content.
        panel_lay = QVBoxLayout(self.panel)
        panel_lay.setContentsMargins(0, 0, 0, 0)
        panel_lay.setSpacing(0)

        # Сетка 8 px: внешние поля 16, промежутки между блоками 16.
        lay = QVBoxLayout()
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(16)

        # ---------------- шапка ----------------
        head_bar = QFrame()
        head_bar.setObjectName("headerBar")
        hb = QHBoxLayout(head_bar)
        hb.setContentsMargins(16, 8, 16, 8)
        hb.setSpacing(8)

        title_box = QHBoxLayout()
        title_box.setSpacing(8)
        ico = QLabel()
        ico.setPixmap(self._icon("film").pixmap(20, 20))
        ico.setFixedSize(20, 20)
        ico.setStyleSheet("background: transparent;")
        title_box.addWidget(ico)
        # Заголовок ЛАТИНИЦЕЙ намеренно: на русской Windows в панели задач
        # и Alt+Tab кириллица из UTF-8 иногда превращается в иероглифы
        # («Ш‹_Б-В-А-Г…»), а латиница в любой кодировке читается верно.
        title = QLabel("GTA Video Overlay")
        title.setObjectName("title")
        title_box.addWidget(title)
        hb.addLayout(title_box)
        hb.addStretch(1)

        # бейдж-«пилюля»: фон + текст одного цвета состояния
        self.lbl_status = QLabel("●  прокси: запуск…")
        self.lbl_status.setObjectName("badge")
        self.lbl_status.setProperty("state", "idle")
        self.lbl_status.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lbl_status.setToolTip("Нажмите, чтобы посмотреть лог прокси")
        # бейдж не сжимается: текст не должен обрезаться
        self.lbl_status.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.lbl_status.mousePressEvent = lambda *_: self._show_proxy_log()  # type: ignore[method-assign]
        hb.addWidget(self.lbl_status)

        self.btn_restart = QPushButton()
        self.btn_restart.setObjectName("ghost")
        self.btn_restart.setIcon(self._icon("refresh"))
        self.btn_restart.setFixedSize(32, 32)
        self.btn_restart.setToolTip("Перезапустить прокси")
        self.btn_restart.clicked.connect(self._restart_proxy)
        hb.addWidget(self.btn_restart)

        btn_min = QPushButton()
        btn_min.setObjectName("ghost")
        btn_min.setIcon(self._icon("minimize"))
        btn_min.setFixedSize(32, 32)
        btn_min.setToolTip("Свернуть")
        btn_min.clicked.connect(self.showMinimized)
        hb.addWidget(btn_min)

        btn_close = QPushButton()
        btn_close.setObjectName("danger")
        btn_close.setIcon(self._icon("close"))
        btn_close.setFixedSize(32, 32)
        btn_close.setToolTip("Закрыть")
        btn_close.clicked.connect(self._quit)
        hb.addWidget(btn_close)

        self._head_bar = head_bar

        # ---------------- сообщения ----------------
        # Подсказки, ошибки и лог живут в ОТДЕЛЬНОМ окне (NoteWindow):
        # в панели длинный текст не влезал и обрезался снизу.
        # info_bar оставлен пустым «якорем» для совместимости — он невидим
        # и не занимает места.
        self.info_bar = QWidget()
        self.info_bar.setVisible(False)
        self.info_bar.setFixedHeight(0)
        self.lbl_info = QLabel("")
        self.lbl_info.setVisible(False)
        self.btn_info_copy = QPushButton()
        self.btn_info_copy.setVisible(False)
        self.btn_info_retry = QPushButton()
        self.btn_info_retry.setVisible(False)

        # ---------------- ввод ссылки ----------------
        # отдельный контейнер с промежутком 8: заголовок, поле и подсказка
        # стоят плотно, а не «плавают» в общих 16 px
        url_group = QVBoxLayout()
        url_group.setContentsMargins(0, 0, 0, 0)
        url_group.setSpacing(8)

        self.lbl_url_title = QLabel("Ссылка на видео")
        self.lbl_url_title.setObjectName("sectionTitle")
        self.lbl_url_title.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.lbl_url_title.setToolTip(
            "Поддерживаются: YouTube, VK Видео, Rutube, Twitch, Kodik, "
            "аниме-каталоги, прямые ссылки .m3u8 / .mp4 и обычные страницы "
            "с видео.\n\n"
            "Вставьте ссылку на видео или на страницу с ним и нажмите "
            "«Открыть на ТВ» — приложение само найдёт поток."
        )

        self.inp_url = QLineEdit()
        # короткий плейсхолдер: длинный обрезался и выглядел неаккуратно
        self.inp_url.setPlaceholderText("Вставьте ссылку на видео…")
        self.inp_url.setClearButtonEnabled(True)
        self.inp_url.returnPressed.connect(self._open_on_tv)

        # примеры форматов — мелким тусклым текстом ПОД полем
        self.lbl_url_hint = QLabel(URL_HINT_DEFAULT)
        self.lbl_url_hint.setObjectName("caption")
        self.lbl_url_hint.setProperty("state", "idle")
        self.lbl_url_hint.setWordWrap(False)

        # Режим определяется сам (см. _guess_mode): пользователю выбирать не нужно.
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("YouTube (кеш, вечная ссылка)", "yt")
        self.cmb_mode.addItem("Любой сайт (поток)", "r")
        self.cmb_mode.addItem("Прямой файл (прокси)", "get")
        self.cmb_mode.setVisible(False)  # скрыт: режим ставится автоматически

        # ---------------- действия ----------------
        # Главное и единственное действие — «Открыть на ТВ» (Primary, заливка).
        # Кнопки «Из моего браузера» нет: удалена по запросу, вместе с кодом.
        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)

        self.btn_tv = QPushButton("  Открыть на ТВ")
        self.btn_tv.setIcon(self._icon("tv"))
        self.btn_tv.setObjectName("primary")
        self.btn_tv.setToolTip("Создать страницу-плеер с управлением и скопировать ссылку")
        self.btn_tv.clicked.connect(self._open_on_tv)
        actions_row.addWidget(self.btn_tv, 2)

        # Кнопка «?» убрана: подсказка висит на самом заголовке поля —
        # отдельная иконка в углу выглядела потерянной.
        self.btn_help = None

        # ---------------- готовая ссылка ----------------
        self.out_box = QFrame()
        self.out_box.setObjectName("card")
        out_lay = QVBoxLayout(self.out_box)
        out_lay.setContentsMargins(16, 16, 16, 16)
        out_lay.setSpacing(12)

        out_head = QHBoxLayout()
        out_head.setSpacing(8)
        lbl_out_title = QLabel("Ссылка для телевизора в игре")
        lbl_out_title.setObjectName("sectionTitle")
        out_head.addWidget(lbl_out_title)
        out_head.addStretch(1)
        out_lay.addLayout(out_head)

        # поле «только чтение» + кнопки ВНУТРИ одного контейнера:
        # поле занимает всё место, кнопки прижаты к правому краю (spacing = 0)
        field_group = QFrame()
        field_group.setObjectName("fieldGroup")
        field_row = QHBoxLayout(field_group)
        field_row.setContentsMargins(0, 0, 0, 0)
        field_row.setSpacing(0)

        self.lbl_out = QLineEdit()
        self.lbl_out.setReadOnly(True)
        self.lbl_out.setPlaceholderText("Здесь появится готовая ссылка")
        self.lbl_out.setObjectName("outField")
        field_row.addWidget(self.lbl_out, 1)

        self.btn_copy_inline = QPushButton()
        self.btn_copy_inline.setIcon(self._icon("copy", 18))
        self.btn_copy_inline.setObjectName("inlineCopy")
        self.btn_copy_inline.setFixedSize(40, 36)
        self.btn_copy_inline.setToolTip("Скопировать ссылку")
        self.btn_copy_inline.clicked.connect(self._copy_result)
        field_row.addWidget(self.btn_copy_inline, 0)

        # «Посмотреть в браузере» — проверить ссылку до телевизора.
        # Если тут видео идёт, а в игре нет — проблема в игре, не в ссылке.
        self.btn_open_browser = QPushButton()
        self.btn_open_browser.setIcon(self._icon("globe", 18))
        self.btn_open_browser.setObjectName("inlineCopy")
        self.btn_open_browser.setFixedSize(40, 36)
        self.btn_open_browser.setToolTip("Посмотреть в браузере")
        self.btn_open_browser.clicked.connect(self._open_in_browser)
        field_row.addWidget(self.btn_open_browser, 0)

        out_lay.addWidget(field_group)

        # История больше не нужна: ссылки открываются сразу.
        self.lst = QListWidget()
        self.lst.setVisible(False)

        # ---------------- пульт ----------------
        self.remote_box = QFrame()
        self.remote_box.setObjectName("card")
        rb = QVBoxLayout(self.remote_box)
        rb.setContentsMargins(16, 16, 16, 16)
        rb.setSpacing(12)

        # шапка карточки: состояние слева, выбор качества справа
        remote_head = QHBoxLayout()
        remote_head.setSpacing(8)

        self.lbl_remote = QLabel("Пульт: нет активного видео")
        self.lbl_remote.setObjectName("hint")
        self.lbl_remote.setProperty("state", "idle")
        remote_head.addWidget(self.lbl_remote, 1)

        lbl_q = QLabel("Качество")
        lbl_q.setObjectName("caption")
        remote_head.addWidget(lbl_q)

        self._filling_quality = False
        self._filling_trans = False
        self.cmb_quality = QComboBox()
        self.cmb_quality.setFixedWidth(104)
        self.cmb_quality.setToolTip("Качество видео (для потоковых сайтов: Rutube, VK, Twitch)")
        self.cmb_quality.addItem("Авто", -1)
        self.cmb_quality.setEnabled(False)
        self.cmb_quality.activated.connect(self._remote_quality)
        remote_head.addWidget(self.cmb_quality)
        rb.addLayout(remote_head)

        # выбор озвучки
        tr_row = QHBoxLayout()
        tr_row.setSpacing(8)
        self.lbl_tr = QLabel("Озвучка")
        self.lbl_tr.setObjectName("caption")
        self.lbl_tr.setFixedWidth(64)
        tr_row.addWidget(self.lbl_tr)
        self.cmb_trans = QComboBox()
        self.cmb_trans.setToolTip("Выбор озвучки (если источник её предлагает)")
        self.cmb_trans.setEnabled(False)
        self.cmb_trans.activated.connect(self._remote_translation)
        tr_row.addWidget(self.cmb_trans, 1)
        rb.addLayout(tr_row)

        # прогресс
        prow = QHBoxLayout()
        prow.setSpacing(8)
        self.lbl_pos = QLabel("0:00")
        self.lbl_pos.setObjectName("mono")
        self.lbl_pos.setMinimumWidth(48)
        self.sld_seek = QSlider(Qt.Orientation.Horizontal)
        self.sld_seek.setRange(0, 1000)
        self.sld_seek.setEnabled(False)
        self.sld_seek.sliderReleased.connect(self._remote_seek)
        self.lbl_dur = QLabel("0:00")
        self.lbl_dur.setObjectName("mono")
        self.lbl_dur.setMinimumWidth(48)
        prow.addWidget(self.lbl_pos)
        prow.addWidget(self.sld_seek, 1)
        prow.addWidget(self.lbl_dur)
        rb.addLayout(prow)

        # кнопки управления: все прозрачные, отклик при наведении
        brow = QHBoxLayout()
        brow.setSpacing(4)

        def mk_btn(text, tip, slot, accent=False):
            b = QPushButton(text)
            b.setObjectName("pad")
            if accent:
                b.setProperty("accent", "true")
            b.setToolTip(tip)
            b.clicked.connect(slot)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            # размеры задаёт style.qss (48x48, главная 56x56) — иконка внутри
            b.setIconSize(QSize(26 if accent else 20, 26 if accent else 20))
            return b

        self.btn_back10 = mk_btn("", "−10 секунд", lambda: self._remote_cmd("seekRel", -10))
        self.btn_back10.setIcon(self._icon("back10"))
        self.btn_play = mk_btn("", "Пуск / пауза", lambda: self._remote_cmd("toggle"), accent=True)
        self.btn_play.setIcon(self._icon("play"))
        self.btn_fwd10 = mk_btn("", "+10 секунд", lambda: self._remote_cmd("seekRel", 10))
        self.btn_fwd10.setIcon(self._icon("fwd10"))
        self.btn_mute = mk_btn("", "Звук вкл/выкл", lambda: self._remote_cmd("mute", None))
        self.btn_mute.setIcon(self._icon("volume"))
        self.btn_subs = mk_btn("", "Субтитры", lambda: self._remote_cmd("subs", None))
        self.btn_subs.setIcon(self._icon("cc"))
        self.btn_full = mk_btn(
            "", "Развернуть / свернуть", lambda: self._remote_cmd("fullscreen", None)
        )
        self.btn_full.setIcon(self._icon("fullscreen"))
        # стоп — не красный: красный означает «запись»; иконка квадрата
        self.btn_stop = mk_btn(
            "", "Остановить и начать сначала", lambda: self._remote_cmd("restart")
        )
        self.btn_stop.setObjectName("padStop")  # при наведении краснеет
        self.btn_stop.setIcon(self._icon("stop"))
        for b in (
            self.btn_back10,
            self.btn_play,
            self.btn_fwd10,
            self.btn_mute,
            self.btn_subs,
            self.btn_full,
            self.btn_stop,
        ):
            brow.addWidget(b)

        # ползунок громкости создаётся ниже, рядом с кнопками
        self.sld_vol = QSlider(Qt.Orientation.Horizontal)
        self.sld_vol.setRange(0, 100)
        self.sld_vol.setValue(100)
        self.sld_vol.setToolTip("Громкость")
        self.sld_vol.sliderMoved.connect(self._remote_volume)

        # громкость: после кнопок, ползунок длиннее — удобнее тянуть
        brow.addSpacing(8)
        self.sld_vol.setFixedWidth(88)
        self.sld_vol.setFixedHeight(20)
        brow.addWidget(self.sld_vol, 0, Qt.AlignmentFlag.AlignVCenter)
        brow.addStretch(1)
        rb.addLayout(brow)

        # ---------------- активные сессии ----------------
        self.sess_box = QFrame()
        self.sess_box.setObjectName("card")
        sb = QVBoxLayout(self.sess_box)
        sb.setContentsMargins(16, 16, 16, 16)
        sb.setSpacing(8)

        head_row = QHBoxLayout()
        head_row.setSpacing(8)
        head_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        lbl_sess = QLabel("Сейчас запущено")
        lbl_sess.setObjectName("sectionTitle")
        head_row.addWidget(lbl_sess, 0, Qt.AlignmentFlag.AlignVCenter)

        # подсказка отделена разделителем и приглушена, чтобы не сливаться
        # с названием трека в списке
        sep = QFrame()
        sep.setObjectName("vdivider")
        sep.setFixedSize(1, 16)
        head_row.addWidget(sep, 0, Qt.AlignmentFlag.AlignVCenter)

        lbl_sess_hint = QLabel("двойной клик — вернуть пульт")
        lbl_sess_hint.setObjectName("faint")
        head_row.addWidget(lbl_sess_hint, 0, Qt.AlignmentFlag.AlignVCenter)
        head_row.addStretch(1)

        btn_sess_refresh = QPushButton()
        btn_sess_refresh.setIcon(self._icon("refresh", 18))
        btn_sess_refresh.setObjectName("ghost")
        btn_sess_refresh.setFixedSize(32, 32)
        btn_sess_refresh.setToolTip("Обновить список")
        btn_sess_refresh.clicked.connect(self._refresh_sessions)
        head_row.addWidget(btn_sess_refresh, 0, Qt.AlignmentFlag.AlignVCenter)
        sb.addLayout(head_row)

        self.lst_sess = QListWidget()
        self.lst_sess.setMaximumHeight(96)
        self.lst_sess.itemDoubleClicked.connect(self._switch_session)
        self.lst_sess.setToolTip("Двойной клик по строке — вернуть управление этим видео")
        sb.addWidget(self.lst_sess)
        self.lst_sess.setVisible(False)

        self._sessions_cache: list[dict] = []

        # ---------------- закреплённые ссылки ----------------
        # Постоянные ссылки /v/<id>.mp4: файл лежит локально и не протухает,
        # даже если исходный .m3u8 жил только в сессии браузера.
        self.pin_box = QFrame()
        self.pin_box.setObjectName("card")
        self.pin_box.setVisible(False)
        pb = QVBoxLayout(self.pin_box)
        pb.setContentsMargins(16, 16, 16, 16)
        pb.setSpacing(8)

        pin_head = QHBoxLayout()
        pin_head.setSpacing(8)
        lbl_pin = QLabel("Закреплённые ссылки")
        lbl_pin.setObjectName("sectionTitle")
        pin_head.addWidget(lbl_pin, 0, Qt.AlignmentFlag.AlignVCenter)
        sep_pin = QFrame()
        sep_pin.setObjectName("vdivider")
        sep_pin.setFixedSize(1, 16)
        pin_head.addWidget(sep_pin, 0, Qt.AlignmentFlag.AlignVCenter)
        self.lbl_pin_hint = QLabel("постоянные · не протухают")
        self.lbl_pin_hint.setObjectName("faint")
        pin_head.addWidget(self.lbl_pin_hint, 0, Qt.AlignmentFlag.AlignVCenter)
        pin_head.addStretch(1)
        btn_pin_refresh = QPushButton()
        btn_pin_refresh.setIcon(self._icon("refresh", 18))
        btn_pin_refresh.setObjectName("ghost")
        btn_pin_refresh.setFixedSize(32, 32)
        btn_pin_refresh.setToolTip("Обновить список")
        btn_pin_refresh.clicked.connect(self._refresh_pins)
        pin_head.addWidget(btn_pin_refresh, 0, Qt.AlignmentFlag.AlignVCenter)
        pb.addLayout(pin_head)

        self.lst_pins = QListWidget()
        self.lst_pins.setMaximumHeight(96)  # уточняется в _apply_pins
        self.lst_pins.itemDoubleClicked.connect(self._open_pin)
        self.lst_pins.setToolTip("Двойной клик — открыть на телевизоре")
        pb.addWidget(self.lst_pins)

        pin_row = QHBoxLayout()
        pin_row.setSpacing(8)
        b_pin_open = QPushButton("  Открыть на ТВ")
        b_pin_open.setIcon(self._icon("tv", 16))
        b_pin_open.setObjectName("primary")
        b_pin_open.clicked.connect(lambda: self._open_pin(self.lst_pins.currentItem()))
        pin_row.addWidget(b_pin_open)
        b_pin_del = QPushButton("Удалить")
        b_pin_del.setObjectName("ghost")
        b_pin_del.clicked.connect(self._remove_pin)
        pin_row.addWidget(b_pin_del)
        pin_row.addStretch(1)
        pb.addLayout(pin_row)

        # ---------------- нижняя панель ----------------
        footer = QFrame()
        footer.setObjectName("card")
        fr = QHBoxLayout(footer)
        fr.setContentsMargins(16, 8, 16, 8)
        fr.setSpacing(16)
        fr.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.chk_top = QCheckBox("Поверх игры")
        self.chk_top.setChecked(True)
        self.chk_top.toggled.connect(self._toggle_topmost)
        fr.addWidget(self.chk_top, 0, Qt.AlignmentFlag.AlignVCenter)

        self.chk_compact = QCheckBox("Компактно")
        self.chk_compact.toggled.connect(self._toggle_compact)
        fr.addWidget(self.chk_compact, 0, Qt.AlignmentFlag.AlignVCenter)

        fr.addStretch(1)

        lbl_op = QLabel("Прозрачность")
        lbl_op.setObjectName("caption")
        fr.addWidget(lbl_op, 0, Qt.AlignmentFlag.AlignVCenter)

        self.sld_opacity = QSlider(Qt.Orientation.Horizontal)
        self.sld_opacity.setRange(35, 100)
        self.sld_opacity.setValue(95)
        self.sld_opacity.setFixedWidth(96)
        self.sld_opacity.setToolTip("Прозрачность окна")
        self.sld_opacity.valueChanged.connect(lambda v: self.setWindowOpacity(v / 100))
        fr.addWidget(self.sld_opacity, 0, Qt.AlignmentFlag.AlignVCenter)

        # кнопка «В игре» отделена — это важное действие
        self.btn_in_game = QPushButton("  В игре")
        self.btn_in_game.setIcon(self._icon("play", 18))
        self.btn_in_game.setObjectName("primary")
        self.btn_in_game.setToolTip("Скопировать ссылку для телевизора в игре")
        self.btn_in_game.clicked.connect(self._copy_result)
        fr.addWidget(self.btn_in_game, 0, Qt.AlignmentFlag.AlignVCenter)

        # ---------------- сборка ----------------
        # шапка — вне content: растягивается на всю ширину панели
        panel_lay.addWidget(head_bar)

        content = QWidget()
        content.setObjectName("content")
        content.setLayout(lay)
        panel_lay.addWidget(content)

        # заголовок и примеры форматов — одной строкой: блок ниже на ~20 px
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(self.lbl_url_title)
        title_row.addWidget(self.lbl_url_hint, 1)
        # Диагностика: показать журнал прокси и снимок окружения.
        # Нужна, когда «у меня не работает» — по логу видно причину.
        self.btn_diag = QPushButton()
        self.btn_diag.setIcon(self._icon("help", 18))
        self.btn_diag.setObjectName("ghost")
        self.btn_diag.setFixedSize(28, 28)
        self.btn_diag.setToolTip("Диагностика: журнал и состояние")
        self.btn_diag.clicked.connect(self._show_diagnostics)
        title_row.addWidget(self.btn_diag, 0, Qt.AlignmentFlag.AlignVCenter)

        url_group.addLayout(title_row)
        url_group.addWidget(self.inp_url)
        url_group.addWidget(self.cmb_mode)

        lay.addLayout(url_group)
        lay.addLayout(actions_row)

        # ВАЖНО: у всех карточек Preferred/Fixed по вертикали и НУЛЕВОЙ
        # коэффициент растяжения. Иначе QVBoxLayout раздаёт «лишнюю» высоту
        # первой карточке (у неё оставался stretch 1), и при скрытии других
        # блоков она раздувается на всё окно — именно это видно на скриншоте
        # «Сейчас запущено» высотой в треть экрана.
        self._cards = [self.out_box, self.remote_box, self.pin_box, self.sess_box, footer]
        for card in self._cards:
            card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            lay.addWidget(card, 0)

        # единственный «пожиратель» лишней высоты — распорка в конце,
        # чтобы карточки никогда не делили между собой свободное место
        lay.addStretch(1)

        # ресайз-уголок
        grip_row = QHBoxLayout()
        grip_row.addStretch(1)
        grip_row.addWidget(QSizeGrip(self.panel))
        lay.addLayout(grip_row)

        # показать на экране с игрой
        self._move_to_game_screen()

    # ---------- прокси ----------
    def _boot_proxy(self, port: int = DEFAULT_PROXY_PORT) -> None:
        ok = self.proxy.start(port)
        if ok:
            if self.proxy.external:
                self.status_signal.emit(f"уже запущен на :{self.proxy.port}")
            else:
                self.status_signal.emit(f"готов на :{self.proxy.port}")
        else:
            reason = self.proxy.last_error or "неизвестная причина"
            self.status_signal.emit("ОШИБКА — нажмите для лога")
            self.proxy_error = reason
            # показать окно с причиной, не блокируя поток
            QTimer.singleShot(300, self._show_proxy_error)

    def _show_proxy_error(self) -> None:
        reason = getattr(self, "proxy_error", "") or self.proxy.last_error
        self.show_info(
            f"<b>Прокси не запустился</b><br>"
            f"Причина: {reason}<br><br>"
            "Что попробовать:<br>"
            "1. Нажать ⟳ рядом со статусом<br>"
            "2. Запустить run_console.bat и посмотреть ошибки<br>"
            "3. Проверить, свободен ли порт 9000:<br>"
            "<code>netstat -ano | findstr :9000</code>",
            kind="error",
        )

    def _show_proxy_log(self) -> None:
        """
        Лог прокси — в отдельном окне с прокруткой.

        В панели он обрезался: там видно 3–4 строки. Здесь — весь хвост,
        моноширинным шрифтом, с кнопкой «Копировать».
        """
        tail = self.proxy.log_tail(80)
        safe = (
            tail.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )
        path = getattr(self.proxy, "LOG_PATH", None)
        name = path.name if path else "proxy.log"
        self.show_info(
            f"<span style='color:#6E7488'>{name} · последние 80 строк</span>"
            f"<br><br><span style='font-family:Consolas,monospace;font-size:11px'>"
            f"{safe or 'лог пуст'}</span>",
            kind="info",
            copy_text=tail,
            title="Лог прокси",
        )

    def _restart_proxy(self) -> None:
        self.proxy.stop()
        self.lbl_status.setText("●  прокси: запуск…")
        threading.Thread(target=self._boot_proxy, args=(DEFAULT_PROXY_PORT,), daemon=True).start()

    def _refresh_status(self) -> None:
        alive = self.proxy.is_alive()
        if alive:
            port = self.proxy.port
            suffix = " · внешний" if self.proxy.external else ""
            self.lbl_status.setText(f"●  прокси {port}{suffix}")
            self._status_state("ok")
            if port != DEFAULT_PROXY_PORT:
                self.lbl_status.setToolTip(f"Порт 9000 занят, использую {port}. Нажмите для лога")
            else:
                self.lbl_status.setToolTip("Нажмите, чтобы посмотреть лог прокси")
            # порт мог смениться — обновляем историю, чтобы ссылки были рабочими
            if getattr(self, "_rendered_port", None) != port:
                self._rendered_port = port
                if hasattr(self, "lst"):
                    self._apply_history()
        else:
            self.lbl_status.setText("●  прокси: остановлен")
            self._status_state("error")
            self.lbl_status.setToolTip(
                (self.proxy.last_error or "причина неизвестна") + " — нажмите для лога"
            )

    def _set_status(self, text: str) -> None:
        self.lbl_status.setText(f"●  прокси: {text}")

    # ---------- действия ----------
    def _base_url(self) -> str:
        return f"http://127.0.0.1:{self.proxy.port}"

    @staticmethod
    def _guess_mode(url: str) -> str:
        """
        Определить режим по самой ссылке — пользователю выбирать не нужно.

        • .m3u8 / .mp4 / .webm / .mpd  -> прямой файл
        • youtube/youtu.be             -> YouTube
        • всё остальное                -> любой сайт (там найдём поток)
        """
        low = url.lower()
        if any(ext in low for ext in (".m3u8", ".mp4", ".webm", ".mpd", ".mov")):
            return "get"
        if "youtube.com" in low or "youtu.be" in low:
            return "yt"
        return "r"

    def _apply_mode(self) -> str:
        """Выставить скрытый список режима по текущей ссылке."""
        url = self.inp_url.text().strip()
        mode = self._guess_mode(url) if url else "r"
        idx = self.cmb_mode.findData(mode)
        if idx >= 0:
            self.cmb_mode.setCurrentIndex(idx)
        return mode

    def _direct_for(self, h: dict) -> str:
        """Собрать прямую ссылку по текущему порту прокси (порт мог смениться)."""
        url = h.get("url", "")
        mode = h.get("mode", "r")
        if not url:
            return h.get("direct", "")
        return f"{self._base_url()}/{mode}?url={urllib.parse.quote(url, safe='')}"

    def _apply_history(self) -> None:
        self.lst.clear()
        for h in self.history:
            item = QListWidgetItem(f"{h.get('mode', '?')}  •  {h.get('title', h['url'])[:58]}")
            direct = self._direct_for(h)
            item.setData(Qt.ItemDataRole.UserRole, direct)
            item.setToolTip(direct)
            self.lst.addItem(item)

    def _show_diagnostics(self) -> None:
        """
        Показать журнал диагностики: что запрашивалось, что вернул CDN,
        какие ошибки прислала игра, состояние сети и ffmpeg.

        Это ответ на «у меня не работает, а в тестах работает»: вместо
        догадок — факты из журнала.
        """

        def worker() -> None:
            try:
                with urllib.request.urlopen(f"{self._base_url()}/diag?limit=400", timeout=15) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self.diag_signal.emit(data)
            except Exception as e:
                self.diag_signal.emit({"error": str(e)})

        threading.Thread(target=worker, daemon=True).start()

    def _show_diag_window(self, data: dict) -> None:
        if data.get("error"):
            self.show_info(
                f"Прокси не ответил: {data['error']}<br><br>"
                "Значит он не запущен или упал. Нажмите ⟳ рядом со статусом.",
                kind="error",
                title="Диагностика",
            )
            return

        snap = data.get("snapshot") or {}
        events = data.get("events") or []

        snap_html = "".join(
            f"<tr><td style='color:#9AA0B4;padding-right:12px'>{k}</td><td>{v}</td></tr>"
            for k, v in snap.items()
        )

        rows = []
        for e in events[-200:]:
            stamp = time.strftime("%H:%M:%S", time.localtime(e.get("t", 0)))
            kind = e.get("kind", "")
            colour = {
                "err": "#FF9B9B",
                "warn": "#E8C05C",
                "player": "#FFB37A",
                "boot": "#7FD6A8",
                "new": "#8FB8FF",
            }.get(kind, "#9AA0B4")
            text = str(e.get("text", ""))
            text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            rows.append(
                f"<span style='color:#6E7488'>{stamp}</span> "
                f"<span style='color:{colour}'>{text}</span>"
            )
        events_html = "<br>".join(rows) or "событий пока нет"

        html = (
            f"<table cellspacing='3'>{snap_html}</table>"
            f"<br><br><b>Журнал событий</b> ({len(events)})<br>"
            f"<span style='font-family:Consolas,monospace;font-size:11px'>"
            f"{events_html}</span>"
        )

        # текст для копирования — то же, но без разметки
        self.show_info(html, kind="info", title="Диагностика", copy_text=data.get("report") or "")

    def _open_in_browser(self) -> None:
        """Открыть готовую ссылку в системном браузере (для проверки)."""
        link = self.lbl_out.text().strip()
        if not link:
            self.show_info(
                "Сначала нажмите «Открыть на ТВ» — появится ссылка, "
                "которую можно проверить в браузере.",
                kind="info",
                title="Посмотреть в браузере",
            )
            return
        ok = QDesktopServices.openUrl(QUrl(link))
        if not ok:
            self.show_info(
                f"Не удалось открыть браузер.<br>Скопируйте ссылку вручную:"
                f"<br><span style='font-family:Consolas,monospace'>{link}</span>",
                kind="warning",
                title="Посмотреть в браузере",
                copy_text=link,
            )

    def _copy_result(self) -> None:
        direct = getattr(self, "current_direct", "") or self.lbl_out.text()
        if direct.startswith("http"):
            QApplication.clipboard().setText(direct)
            self.lbl_out.setText(direct)
            QApplication.beep()
            self._flash("Скопировано!")

    def _flash(self, text: str) -> None:
        """Короткое подтверждение — не затирая саму ссылку в поле."""
        self.lbl_url_hint.setText(f"●  {text}")
        self._set_state(self.lbl_url_hint, "ok")
        QTimer.singleShot(1600, self._reset_hint)

    def _reset_hint(self) -> None:
        self.lbl_url_hint.setText(URL_HINT_DEFAULT)
        self._set_state(self.lbl_url_hint, "idle")

    # ---------- пульт управления видео ----------
    def _open_on_tv(self) -> None:
        """Создать сессию страницы-плеера и положить её ссылку в буфер обмена."""
        url = self.inp_url.text().strip() or getattr(self, "_last_url", "")
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        mode = self._apply_mode()

        self.player_sid = ""
        self.info_bar.setVisible(False)  # убрать старую ошибку
        self.lbl_remote.setText("Пульт: проверяю ссылку…")
        self._remote_state("warn")
        self._remote_status("preparing")

        def worker() -> None:
            try:
                q = urllib.parse.urlencode(
                    {
                        "url": url,
                        "mode": mode,
                        "title": url[:80],
                    }
                )
                req = urllib.request.Request(f"{self._base_url()}/new?{q}", method="GET")
                # Таймаут с запасом: сервер проверяет ссылку максимум 40 сек
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.loads(r.read().decode("utf-8"))

                if not data.get("ok", True):
                    # сервер объяснил, почему ссылка не подойдёт
                    self.error_signal.emit(data.get("reason", "ссылка не подошла"))
                    return

                self.player_url = data.get("player", "")
                self.player_sid = data.get("sid", "")
                self.session_signal.emit(self.player_url, self.player_sid)
                warn = data.get("warning") or ""
                if warn:
                    self.status_signal.emit(f"предупреждение: {warn[:40]}")
            except urllib.error.URLError as e:
                self.error_signal.emit(
                    f"прокси не отвечает — нажмите ⟳ рядом со статусом ({getattr(e, 'reason', e)})"
                )
            except TimeoutError:
                self.error_signal.emit("проверка ссылки заняла больше 60 секунд")
            except Exception as e:
                self.error_signal.emit(str(e) or type(e).__name__)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    #        ЗАКРЕПЛЕНИЕ ССЫЛОК (постоянные ссылки вместо «сессионных»)
    # ------------------------------------------------------------------
    def _pin_done(self, data: dict) -> None:
        if not data.get("ok"):
            self.show_info(
                f"<b>Не удалось закрепить</b><br>{data.get('error', '')}<br><br>"
                "Убедитесь, что ссылка ещё жива: закреплять лучше сразу после "
                "того, как поймали поток (пока не истёк токен).",
                kind="error",
                title="Закрепление ссылки",
            )
            return
        link = data.get("link") or ""
        self._show_card(self.pin_box)
        self._refresh_pins()
        # сразу кладём в поле и буфер: ссылка уже рабочая (файл докачается)
        self.lbl_out.setText(link)
        QApplication.clipboard().setText(link)
        self.show_info(
            f"<b>Ссылка закреплена</b><br>"
            f"<span style='font-family:Consolas,monospace'>{link}</span><br><br>"
            "Она уже скопирована и вставлена в поле для телевизора — можно "
            "включать прямо сейчас, файл докачается по ходу.<br><br>"
            "<b>Почему это работает:</b> прокси запомнил заголовки и cookie "
            "вашего браузера (по ним CDN отдавал поток) и скачал файл к вам на "
            "диск. Дальше ссылка живёт у вас, а не на сайте, — поэтому не "
            "протухает.",
            kind="success",
            title="Готово",
        )

    def _refresh_pins(self) -> None:
        """Показать закреплённые ссылки и обновить прогресс скачивания."""

        def worker() -> None:
            try:
                with urllib.request.urlopen(f"{self._base_url()}/pins", timeout=4) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self.pins_signal.emit(data)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _apply_pins(self, data: dict) -> None:
        rows = data.get("pins") or []
        self._pins_cache = rows
        if not rows:
            if self.pin_box.isVisible():
                self._hide_card(self.pin_box)
            return
        if not self.pin_box.isVisible():
            self._show_card(self.pin_box)
        cur = self.lst_pins.currentRow()
        self.lst_pins.clear()
        for r in rows:
            state = r.get("state") or ""
            size = (r.get("size") or 0) / 1024 / 1024
            if state == "ready":
                mark = f"готово · {size:.0f} МБ"
            elif state == "downloading":
                pct = r.get("progress") or 0
                mark = f"качается {pct}% · {size:.0f} МБ" if pct else f"качается · {size:.0f} МБ"
            elif state == "error":
                mark = "ошибка"
            else:
                mark = "в очереди"
            title = (r.get("title") or "").split("?")[0].rstrip("/").split("/")[-1]
            name = title or r["id"]
            it = QListWidgetItem(f"{name} — {mark}")
            it.setIcon(self._icon("film", 16))
            if state == "ready":
                it.setIcon(self._icon("play", 16))
            it.setData(Qt.ItemDataRole.UserRole, r.get("link") or "")
            it.setData(Qt.ItemDataRole.UserRole + 1, r["id"])
            it.setToolTip(f"{r.get('url', '')}\n{r.get('link', '')}")
            if state != "ready":
                it.setForeground(QColor("#9AA0B4"))
            self.lst_pins.addItem(it)
        if 0 <= cur < self.lst_pins.count():
            self.lst_pins.setCurrentRow(cur)
        elif self.lst_pins.count():
            self.lst_pins.setCurrentRow(0)
        # высота по числу строк (не больше трёх): строки не обрезаются
        rows = min(max(self.lst_pins.count(), 1), 3)
        self.lst_pins.setFixedHeight(rows * 44 + 8)

    def _open_pin(self, item) -> None:
        if item is None:
            return
        link = item.data(Qt.ItemDataRole.UserRole) or ""
        if not link:
            return
        self.lbl_out.setText(link)
        QApplication.clipboard().setText(link)
        self._last_url = link
        self._open_on_tv()

    def _remove_pin(self) -> None:
        item = self.lst_pins.currentItem()
        if item is None:
            return
        pid = item.data(Qt.ItemDataRole.UserRole + 1) or ""
        if not pid:
            return

        def worker() -> None:
            try:
                urllib.request.urlopen(
                    f"{self._base_url()}/unpin?id={urllib.parse.quote(pid)}", timeout=10
                ).read()
            except Exception:
                pass
            self._refresh_pins()

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    #        ПОИСК ПОТОКА НА СТРАНИЦЕ (аналог «Сеть → Медиа»)
    # ------------------------------------------------------------------
    def _session_ready(self, player_url: str, sid: str) -> None:
        self.current_direct = player_url
        # QLineEdit — однострочный: пояснение уходит в подсказку поля
        self.lbl_out.setText(player_url)
        self.lbl_out.setToolTip("Это страница с управлением — вставьте ссылку в телевизор в игре")
        QApplication.clipboard().setText(player_url)
        self.lbl_remote.setText("Пульт: жду, когда телевизор откроет ссылку…")
        self._remote_state("warn")
        self._remote_status("ready")

    def _remote_error(self, msg: str) -> None:
        """Показать ошибку в пульте и в панели сообщений (без модальных окон)."""
        self.player_sid = ""
        self._remote_status("error")
        self.show_info(
            f"<b>Не удалось открыть видео</b><br>{msg}",
            kind="error",
            copy_text=msg,
            retry=True,
        )
        self.lbl_remote.setText(f"Пульт: {msg}")
        self._remote_state("error")
        self.lbl_remote.setToolTip(msg)
        self.btn_play.setEnabled(False)
        self.btn_back10.setEnabled(False)
        self.btn_fwd10.setEnabled(False)
        self.btn_mute.setEnabled(False)
        self.btn_subs.setEnabled(False)
        self.btn_full.setEnabled(False)
        self.sld_seek.setEnabled(False)
        self.sld_vol.setEnabled(False)

    # ---------- встроенные сообщения (вместо модальных окон) ----------
    def show_info(
        self,
        html_text: str,
        kind: str = "info",
        copy_text: str | None = None,
        retry: bool = False,
        title: str = "",
    ) -> None:
        """
        Показать сообщение в ОТДЕЛЬНОМ окне.

        Раньше текст вставлялся внутрь панели — длинные подсказки не влезали
        и обрезались. Теперь окно своё, с прокруткой и кнопками: пульт
        остаётся компактным, а текст читается целиком.
        """
        self._raise_and_focus()
        self.note.show_note(
            html_text, kind=kind, title=title, copy_text=copy_text, retry=retry, anchor=self
        )

    def _raise_and_focus(self) -> None:
        """Гарантированно показать окно поверх игры."""
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()
        # «Встряску» флагов делаем только если окна ещё нет поверх:
        # setWindowFlags пересоздаёт окно и даёт заметное мигание.
        if not (self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint):
            flags = self.windowFlags()
            self.setWindowFlags(flags | Qt.WindowType.WindowStaysOnTopHint)
            self.show()

    def _retry_last(self) -> None:
        self.info_bar.setVisible(False)
        self._open_on_tv()

    def _show_help(self) -> None:
        self.show_info(
            "<b>Как пользоваться</b><br>"
            "1. Вставьте ссылку на видео (YouTube, сайт, файл).<br>"
            "2. Нажмите <b>«Открыть на ТВ»</b> — ссылка скопируется.<br>"
            "3. Вставьте её в телевизор в GTA 5.<br>"
            "4. Управляйте кнопками внизу: −10 / пуск-пауза / +10, "
            "звук, субтитры, полный экран, стоп.<br><br>"
            "<b>Если видео не стартует</b> — один раз кликните по синему "
            "треугольнику на экране телевизора.<br><br>"
            "<b>Окно</b>: тяните за шапку, размер — за правый нижний угол. "
            "Esc больше не скрывает окно.",
            kind="info",
        )

    def _remote_status(self, status: str) -> None:
        """Заблокировать пульт, пока видео не готово."""
        self.remote_status = status
        ready = status == "ready"
        for b in (
            self.btn_play,
            self.btn_back10,
            self.btn_fwd10,
            self.btn_mute,
            self.btn_subs,
            self.btn_full,
            self.btn_stop,
        ):
            b.setEnabled(ready)
        self.sld_seek.setEnabled(ready)
        self.sld_vol.setEnabled(ready)
        self.cmb_quality.setEnabled(ready)
        if status == "preparing":
            self.lbl_pos.setText("0:00")
            self.lbl_dur.setText("0:00")
            self.sld_seek.setValue(0)

    def _remote_cmd(self, cmd: str, value=None) -> None:
        sid = getattr(self, "player_sid", "")
        if not sid:
            self.lbl_remote.setText("Пульт: сначала нажмите «Открыть на ТВ»")
            return
        # переключатели: берём текущее состояние и инвертируем
        if value is None:
            st = getattr(self, "_last_state", {}) or {}
            if cmd == "toggle":
                cmd = "pause" if st.get("playing") else "play"
                value = None
            elif cmd == "mute":
                value = not st.get("muted", False)
            elif cmd == "subs":
                value = not st.get("subs", False)
            elif cmd == "fullscreen":
                value = not st.get("fullscreen", False)

        def worker() -> None:
            try:
                q = urllib.parse.urlencode({"cmd": cmd, "value": "" if value is None else value})
                urllib.request.urlopen(f"{self._base_url()}/control/{sid}?{q}", timeout=8).read()
            except Exception as e:
                # В консоль не пишем: у pythonw.exe её нет. Событие уходит
                # в журнал диагностики — его видно в отчёте пользователя.
                diag.log("err", f"пульт: команда {cmd} не отправлена: {e}")

        threading.Thread(target=worker, daemon=True).start()

    # ---------- возврат к уже запущенному видео ----------
    def _refresh_sessions(self) -> None:
        """Запросить у прокси список всех сессий (в фоне)."""
        if getattr(self, "_sessions_loading", False):
            return
        self._sessions_loading = True

        def worker() -> None:
            try:
                with urllib.request.urlopen(f"{self._base_url()}/sessions", timeout=4) as r:
                    self.sessions_signal.emit(json.loads(r.read().decode("utf-8")))
            except Exception:
                pass
            finally:
                self._sessions_loading = False

        threading.Thread(target=worker, daemon=True).start()

    def _apply_sessions(self, data: dict) -> None:
        items = data.get("sessions") or []
        self._sessions_cache = items
        current = getattr(self, "player_sid", "")
        self.lst_sess.clear()
        for it in items:
            mark = "играет" if it.get("playing") else "пауза"
            live = "●" if it.get("connected") else "○"
            title = (it.get("title") or it.get("url") or "")[:52]
            tr = it.get("translation") or ""
            extra = f" · {tr}" if tr else ""
            item = QListWidgetItem(f"{live} {mark} · {title}{extra}")
            item.setData(Qt.ItemDataRole.UserRole, it.get("sid", ""))
            if it.get("sid") == current:
                # текущая сессия помечается маркером из базового шрифта:
                # «★» есть не во всех системных шрифтах Windows
                item.setText("● " + item.text())
            item.setToolTip(it.get("url", ""))
            self.lst_sess.addItem(item)
        self.lst_sess.setVisible(len(items) > 0)
        # подсказка, если переключились на другое видео
        if len(items) > 1 and current:
            others = [i for i in items if i.get("sid") != current]
            if others:
                self.lbl_remote.setToolTip("Другие запущенные видео — в списке «Сейчас запущено»")

    def _switch_session(self, item: QListWidgetItem) -> None:
        """Вернуть пульт к уже запущенному видео."""
        sid = item.data(Qt.ItemDataRole.UserRole)
        if not sid or sid == getattr(self, "player_sid", ""):
            return
        found = next((s for s in self._sessions_cache if s.get("sid") == sid), None)
        if not found:
            return
        self.player_sid = sid
        self.player_url = f"{self._base_url()}/player/{sid}"
        self.current_direct = self.player_url
        self.lbl_out.setText(self.player_url)
        self.lbl_out.setToolTip("Вернулись к этому видео")
        self.lbl_remote.setText("Пульт: вернулись к выбранному видео")
        self._remote_state("ok")
        self._remote_status("ready")
        self._apply_remote_state({"connected": True})  # сразу включить кнопки
        self._refresh_sessions()

    def _remote_translation(self, idx: int) -> None:
        """Сменить озвучку."""
        if getattr(self, "_filling_trans", False):
            return
        val = self.cmb_trans.itemData(idx)
        if val is None:
            return
        # Запоминаем, что выбрал пользователь, и до подтверждения от
        # страницы не даём опросу /state откатить список назад на первую
        # озвучку (страница присылает старое значение, пока не переключилась).
        self._trans_want: int | None = int(val)
        self._trans_want_at = time.time()
        self._remote_cmd("trans", int(val))

    def _refresh_translation_list(self, st: dict) -> None:
        items = st.get("translations") or []
        current = st.get("translationIndex", 0)

        # Только что переключили озвучку, но страница ещё не успела
        # применить команду — держим выбор пользователя, а не откатываем.
        # _trans_want хранит индекс озвучки, выбранной пользователем, пока
        # страница не подтвердила команду. Здесь только читаем его.
        want = getattr(self, "_trans_want", None)
        if isinstance(want, int):
            if current == want:
                self._trans_want = None  # страница подтвердила выбор
            elif time.time() - getattr(self, "_trans_want_at", 0) < 8:
                current = want  # держим выбор пользователя
            else:
                self._trans_want = None  # не получилось — доверяем странице
        if not items:
            self.cmb_trans.clear()
            self.cmb_trans.setEnabled(False)
            self.lbl_tr.setVisible(False)
            self.cmb_trans.setVisible(False)
            return
        self.lbl_tr.setVisible(True)
        self.cmb_trans.setVisible(True)
        self.cmb_trans.setEnabled(len(items) > 1)
        have = [self.cmb_trans.itemText(i) for i in range(self.cmb_trans.count())]
        if have == list(items):
            if self.cmb_trans.currentIndex() != current:
                self._filling_trans = True
                self.cmb_trans.setCurrentIndex(current)
                self._filling_trans = False
            return
        self._filling_trans = True
        self.cmb_trans.clear()
        for i, name in enumerate(items):
            self.cmb_trans.addItem(name, i)
        self.cmb_trans.setCurrentIndex(current if 0 <= current < len(items) else 0)
        self._filling_trans = False

    def _remote_quality(self, idx: int) -> None:
        """Выбрать качество: -1 = авто, иначе индекс из списка."""
        if getattr(self, "_filling_quality", False):
            return
        val = self.cmb_quality.itemData(idx)
        if val is None:
            return
        self._remote_cmd("quality", int(val))

    def _remote_seek(self) -> None:
        st = getattr(self, "_last_state", {}) or {}
        dur = st.get("duration") or 0
        if dur <= 0:
            return
        self._remote_cmd("seek", round(self.sld_seek.value() / 1000 * dur, 1))

    def _remote_volume(self, val: int) -> None:
        self._remote_cmd("volume", round(val / 100, 2))

    def _refresh_quality_list(self, st: dict) -> None:
        """Перестроить выпадающий список качеств, не теряя выбор."""
        qualities = st.get("qualities") or []
        if not qualities:
            if self.cmb_quality.count() > 1:
                self._filling_quality = True
                self.cmb_quality.clear()
                self.cmb_quality.addItem("Авто", -1)
                self._filling_quality = False
            return

        current = [self.cmb_quality.itemText(i) for i in range(self.cmb_quality.count())]
        desired = ["Авто", *qualities]
        if current == desired:
            return

        self._filling_quality = True
        self.cmb_quality.clear()
        self.cmb_quality.addItem("Авто", -1)
        for i, name in enumerate(qualities):
            self.cmb_quality.addItem(name, i)
        auto = st.get("autoQuality", True)
        if auto:
            self.cmb_quality.setCurrentIndex(0)
        else:
            q = (st.get("quality") or "").replace(" (авто)", "")
            idx = self.cmb_quality.findText(q)
            if idx >= 0:
                self.cmb_quality.setCurrentIndex(idx)
        self._filling_quality = False

    def _poll_remote(self) -> None:
        """Опрос состояния активной сессии (вызывается таймером)."""
        sid = getattr(self, "player_sid", "")
        if not sid:
            return

        if getattr(self, "_polling", False):
            return
        self._polling = True

        def worker() -> None:
            try:
                with urllib.request.urlopen(f"{self._base_url()}/state/{sid}", timeout=4) as r:
                    st = json.loads(r.read().decode("utf-8"))
                self.state_signal.emit(st)
            except Exception:
                self.state_signal.emit({"connected": False, "unreachable": True})
            finally:
                self._polling = False

        threading.Thread(target=worker, daemon=True).start()

    def _apply_remote_state(self, st: dict) -> None:
        self._last_state = st
        dur = st.get("duration") or 0
        pos = st.get("currentTime") or 0
        connected = st.get("connected")

        def fmt(t: float) -> str:
            t = max(0, int(t or 0))
            return f"{t // 60}:{t % 60:02d}"

        self.lbl_pos.setText(fmt(pos))
        self.lbl_dur.setText(fmt(dur))
        if not self.sld_seek.isSliderDown() and dur > 0:
            self.sld_seek.setValue(int(pos / dur * 1000))

        if st.get("unreachable"):
            self.lbl_remote.setText("Пульт: прокси не отвечает — нажмите ⟳ рядом со статусом")
            self._remote_state("error")
            return
        if not connected:
            self.lbl_remote.setText("Пульт: жду телевизор — вставьте ссылку из поля выше")
            self._remote_state("idle")
        elif st.get("error"):
            err = st["error"]
            hint = st.get("errorHint") or ""
            self.lbl_remote.setText(f"Пульт: ошибка видео — {err}")
            self._remote_state("error")
            # один раз показать подробности в панели сообщений
            key = f"{err}|{hint}"
            if getattr(self, "_last_err_key", "") != key:
                self._last_err_key = key
                detail = f"<b>{err}</b>"
                if hint:
                    detail += f"<br><br><b>Причина:</b> {hint}"
                detail += (
                    "<br><br><b>Что делать:</b>"
                    "<br>1. Нажать «Повторить» — иногда помогает"
                    "<br>2. Вставить адрес страницы вместо прямой ссылки: "
                    "приложение само найдёт поток"
                    "<br>3. Открыть страницу в браузере и проверить, идёт ли видео там"
                )
                if "frag" in err.lower():
                    detail += (
                        "<br><br><b>Отдельные куски потока не скачались.</b>"
                        "<br>Обычно это временный сбой CDN или слабый канал: "
                        "нажмите «Повторить» или выберите качество ниже "
                        "(«Качество» → 720p вместо Авто)."
                    )
                elif "manifest" in err.lower() or "403" in err or "401" in err:
                    detail += (
                        "<br><br><b>Источник не отдал список частей потока.</b>"
                        "<br>Если ссылка временная и привязана к сессии браузера — "
                        "вставьте её в поле и нажмите <b>«Закрепить»</b>: "
                        "прокси скачает файл к вам, и ссылка станет постоянной."
                    )
                self.show_info(detail, kind="error", retry=True, title="Видео не воспроизводится")
        else:
            state_txt = "играет" if st.get("playing") else "пауза"
            self.lbl_remote.setText(
                f"Пульт: {state_txt}  {fmt(pos)}/{fmt(dur)}  "
                f"звук {int((st.get('volume') or 0) * 100)}%"
            )
            self._remote_state("ok")

        self._refresh_quality_list(st)
        self._refresh_translation_list(st)
        self._refresh_sessions()
        # ВАЖНО: не setText! У кнопок уже есть иконка, и текст рядом с ней
        # выглядел как «дубль кнопки»: иконка плюс символ рядом.
        # Меняем саму иконку — состояние видно, а лишних символов нет.
        self.btn_play.setIcon(self._icon("pause") if st.get("playing") else self._icon("play"))
        self.btn_mute.setIcon(self._icon("muted") if st.get("muted") else self._icon("volume"))
        # включённые режимы подсвечиваются через QSS-свойство active,
        # а не инлайновым стилем — цвет описан в style.qss
        for btn, on in ((self.btn_subs, st.get("subs")), (self.btn_full, st.get("fullscreen"))):
            want = "true" if on else "false"
            if btn.property("active") != want:
                btn.setProperty("active", want)
                btn.style().unpolish(btn)
                btn.style().polish(btn)

    def _hide_card(self, card) -> None:
        """Скрыть карточку и вернуть окно к компактному размеру."""
        card.setVisible(False)
        QTimer.singleShot(0, self._fit_height)

    def _show_card(self, card) -> None:
        """Показать карточку и растянуть окно ровно на её высоту."""
        card.setVisible(True)
        QTimer.singleShot(0, self._fit_height)

    def _fit_height(self) -> None:
        """
        Подогнать высоту окна под содержимое после показа/скрытия блоков.

        Почему вручную: QVBoxLayout при скрытии виджета не уменьшает окно —
        оно остаётся «раздутым», и одиночные карточки растягиваются на всё
        свободное место. Считаем нужную высоту сами: сумма высот видимых
        карточек + отступы + шапка.

        Ширину НЕ трогаем — пользователь мог растянуть окно сам.
        """
        if getattr(self, "_compact", False):
            return
        # дать layout-ам пересчитать размеры после setVisible
        for _ in range(2):
            lay = self.panel.layout()
            if lay is not None:
                lay.activate()
            outer = self.layout()
            if outer is not None:
                outer.activate()
        try:
            need = self.panel.sizeHint().height()
            # рамочная рамка панели: 2 px (border 1px сверху и снизу)
            need += 2
            if need < 200:
                return
            self.resize(self.width(), need)
        except Exception:
            pass

    def _toggle_topmost(self, on: bool) -> None:
        flags = self.windowFlags()
        if on:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()
        if on:
            self._raise_and_focus()

    def _toggle_compact(self, on: bool) -> None:
        self._compact = on
        for w in (self.out_box, self.sess_box, self.pin_box):
            w.setVisible(not on)
        if on:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            self.resize(480, 320)
            self.panel.setMinimumHeight(0)
        else:
            self._fit_height()

    def _quit(self) -> None:
        self.proxy.stop()
        QApplication.quit()

    # ---------- перетаскивание ----------
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._drag_pos is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e) -> None:
        self._drag_pos = None

    def keyPressEvent(self, e) -> None:
        # Esc только отменяет перетаскивание. Окно НЕ скрываем:
        # раньше Esc прятал оверлей, и вернуть его было невозможно.
        if e.key() == Qt.Key.Key_Escape:
            self._drag_pos = None
        elif e.key() == Qt.Key.Key_F1:
            self._show_help()
        else:
            super().keyPressEvent(e)

    def _move_to_game_screen(self) -> None:
        """Разместить оверлей на экране, где обычно открыта игра (последний/правый)."""
        screens = QGuiApplication.screens()
        if screens:
            target = screens[-1].availableGeometry()
            self.move(target.right() - self.width() - 40, target.top() + 60)

    def closeEvent(self, e) -> None:
        self.proxy.stop()
        e.accept()


class _NullStream:
    """Заглушка для stdout/stderr в pythonw.exe (где они = None)."""

    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def reconfigure(self, **_kwargs) -> None:
        pass

    @property
    def encoding(self) -> str:
        return "utf-8"


def _fix_null_streams() -> None:
    """pythonw.exe даёт sys.stdout = sys.stderr = None — print() упадёт с AttributeError."""
    if sys.stdout is None:
        sys.stdout = _NullStream()  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = _NullStream()  # type: ignore[assignment]


def main() -> None:
    _fix_null_streams()
    app = QApplication(sys.argv)
    app.setApplicationName("GTA Video Overlay")
    app.setQuitOnLastWindowClosed(True)

    # глобальный хоткей-заменитель: F8 из окна игры не перехватим без WinAPI,
    # поэтому советуем Alt+Tab / режим «Поверх игры».
    overlay = Overlay()
    overlay.show()

    # print — интерфейс консольного запуска (pythonw.exe даёт stdout=None,
    # поэтому ошибка вывода не должна ронять приложение).
    try:
        print("Оверлей запущен. Закройте окно для выхода.")  # noqa: T201
    except (AttributeError, OSError):
        pass
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
