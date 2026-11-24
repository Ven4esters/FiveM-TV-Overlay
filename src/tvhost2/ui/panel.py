"""Главное окно оверлея v2.

Структура — три вкладки, каждая отвечает за одно:

    Пульт      — вставить ссылку, открыть на ТВ, управлять воспроизведением
    Источники  — состояние всех сайтов (обещано в ТЗ, в v1 так и не было)
    Журнал     — что происходило: первый инструмент для поиска причин

Почему вкладки, а не один длинный экран: заказчик запретил «раздувание».
Пульт занимает ровно столько высоты, сколько нужно; остальное — за
вкладками, где места хватает.

Правила, соблюдённые в этом файле:
  • только layouts, НИГДЕ нет setGeometry/move;
  • QSS по objectName, ни одного инлайнового styleSheet с цветом;
  • иконки — SVG через QIcon, эмодзи нет;
  • кнопки пульта прозрачные, заливка только при наведении;
  • сетка отступов 4/8/16;
  • поля padding 8px 12px, radius 6px, фокус #3B82F6.
"""

from __future__ import annotations

import contextlib
import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__
from . import theme
from .api import Api, ApiError

TAB_PULT = "Пульт"
TAB_SOURCES = "Источники"
TAB_JOURNAL = "Журнал"

STATE_LABELS = {
    "ok": ("работает", "stateOk"),
    "partial": ("с оговоркой", "stateWarn"),
    "needs_cookies": ("нужен вход", "stateWarn"),
    "broken": ("не работает", "stateErr"),
    "browser_only": ("только браузер", "stateWarn"),
}


class Panel(QWidget):
    """Окно оверлея: пульт, источники, журнал."""

    session_ready = Signal(str, str)  # (адрес страницы, sid)
    state_arrived = Signal(dict)
    journal_arrived = Signal(str)
    sources_arrived = Signal(list)
    error_text = Signal(str)
    loudness_arrived = Signal(str)

    def __init__(self, api: Api | None = None) -> None:
        super().__init__()
        self.api = api or Api()
        self.sid = ""
        self.player_url = ""
        self.subs_index = 0
        """Текущая дорожка субтитров: 0 — выключены, N — включена N-я с нуля."""
        self._subtitle_titles: list[str] = []
        """Названия дорожек текущего видео — для подсказки при переключении."""
        self._last_err_key = ""
        self._tabs_built = False

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setWindowIcon(theme.icon("film"))
        self.setMinimumWidth(520)

        self._build()
        self._connect()
        self._restore_tab()
        # QSS ставится на ПРИЛОЖЕНИЕ, а не на это окно: стиль на конкретном
        # виджете конфликтует с application-стилем, и Qt молча применяет
        # только часть правил (кнопка #primary оставалась системной серой).
        # Одна точка применения — application.setStyleSheet() при старте.

    # ------------------------------------------------------------------
    #                              построение
    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.panel_card = QFrame()
        self.panel_card.setObjectName("panel")
        outer.addWidget(self.panel_card)

        card_layout = QVBoxLayout(self.panel_card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        card_layout.addWidget(self._build_header())

        content = QWidget()
        content.setObjectName("content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(theme.GAP_M, theme.GAP_M, theme.GAP_M, theme.GAP_M)
        content_layout.setSpacing(theme.GAP_M)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self.tabs.addTab(self._build_remote_tab(), TAB_PULT)
        self.tabs.addTab(self._build_sources_tab(), TAB_SOURCES)
        self.tabs.addTab(self._build_journal_tab(), TAB_JOURNAL)
        content_layout.addWidget(self.tabs, 1)

        card_layout.addWidget(content, 1)

    def _build_header(self) -> QWidget:
        """Шапка на всю ширину: иконка, название, состояние прокси."""
        bar = QFrame()
        bar.setObjectName("headerBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(theme.GAP_M, theme.GAP_S, theme.GAP_M, theme.GAP_S)
        row.setSpacing(theme.GAP_S)

        icon_label = QLabel()
        icon_label.setPixmap(theme.icon("film").pixmap(20, 20))
        icon_label.setFixedSize(20, 20)
        row.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignVCenter)

        title = QLabel("FiveM Video Overlay")
        title.setObjectName("appTitle")
        row.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)

        self.badge = QLabel("прокси: проверяю…")
        self.badge.setObjectName("badge")
        self.badge.setProperty("state", "idle")
        row.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)

        self.btn_refresh = QPushButton()
        self.btn_refresh.setObjectName("ghost")
        self.btn_refresh.setIcon(theme.icon("refresh", 18))
        self.btn_refresh.setFixedSize(32, 32)
        self.btn_refresh.setToolTip("Проверить связь с прокси")
        row.addWidget(self.btn_refresh, 0, Qt.AlignmentFlag.AlignVCenter)
        return bar

    # ---------------------------- пульт ----------------------------
    def _build_remote_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(theme.GAP_M, theme.GAP_M, theme.GAP_M, theme.GAP_M)
        layout.setSpacing(theme.GAP_M)

        layout.addWidget(self._build_url_block())
        layout.addWidget(self._build_output_block())
        layout.addWidget(self._build_remote_block())
        layout.addStretch(1)
        return page

    def _build_url_block(self) -> QWidget:
        box = QFrame()
        box.setObjectName("card")
        column = QVBoxLayout(box)
        column.setContentsMargins(theme.GAP_M, theme.GAP_M, theme.GAP_M, theme.GAP_M)
        column.setSpacing(theme.GAP_S)

        head = QHBoxLayout()
        head.setSpacing(theme.GAP_S)
        caption = QLabel("Ссылка на видео")
        caption.setObjectName("sectionTitle")
        head.addWidget(caption, 0, Qt.AlignmentFlag.AlignVCenter)

        hint = QLabel("Rutube, VK, YouTube, Twitch, Kodik")
        hint.setObjectName("faint")
        head.addWidget(hint, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)

        help_button = QPushButton()
        help_button.setObjectName("ghost")
        help_button.setIcon(theme.icon("help", 16))
        help_button.setFixedSize(24, 24)
        help_button.setToolTip("Вставьте ссылку на страницу видео или прямую ссылку .m3u8 / .mp4")
        head.addWidget(help_button, 0, Qt.AlignmentFlag.AlignVCenter)
        column.addLayout(head)

        self.input_url = QLineEdit()
        self.input_url.setPlaceholderText("Вставьте ссылку на видео…")
        self.input_url.setClearButtonEnabled(True)
        column.addWidget(self.input_url)

        self.btn_open = QPushButton("Открыть на ТВ")
        self.btn_open.setObjectName("primary")
        self.btn_open.setIcon(theme.icon("play", 18))
        self.btn_open.setToolTip("Разобрать ссылку и получить адрес для телевизора")
        column.addWidget(self.btn_open)
        return box

    def _build_output_block(self) -> QWidget:
        box = QFrame()
        box.setObjectName("card")
        column = QVBoxLayout(box)
        column.setContentsMargins(theme.GAP_M, theme.GAP_M, theme.GAP_M, theme.GAP_M)
        column.setSpacing(theme.GAP_S)

        caption = QLabel("Ссылка для телевизора в игре")
        caption.setObjectName("sectionTitle")
        column.addWidget(caption)

        row = QHBoxLayout()
        row.setSpacing(theme.GAP_S)
        self.output_url = QLineEdit()
        self.output_url.setReadOnly(True)
        self.output_url.setPlaceholderText("Здесь появится готовая ссылка")
        row.addWidget(self.output_url, 1)

        self.btn_copy = QPushButton()
        self.btn_copy.setObjectName("ghost")
        self.btn_copy.setIcon(theme.icon("copy", 18))
        self.btn_copy.setFixedSize(32, 32)
        self.btn_copy.setToolTip("Скопировать ссылку")
        row.addWidget(self.btn_copy)

        self.btn_browser = QPushButton()
        self.btn_browser.setObjectName("ghost")
        self.btn_browser.setIcon(theme.icon("globe", 18))
        self.btn_browser.setFixedSize(32, 32)
        self.btn_browser.setToolTip("Открыть в браузере на этом компьютере")
        row.addWidget(self.btn_browser)
        column.addLayout(row)
        return box

    def _build_remote_block(self) -> QWidget:
        box = QFrame()
        box.setObjectName("card")
        column = QVBoxLayout(box)
        column.setContentsMargins(theme.GAP_M, theme.GAP_M, theme.GAP_M, theme.GAP_M)
        column.setSpacing(theme.GAP_S)

        head = QHBoxLayout()
        head.setSpacing(theme.GAP_S)
        self.remote_status = QLabel("Пульт: вставьте ссылку и нажмите «Открыть на ТВ»")
        self.remote_status.setObjectName("faint")
        self.remote_status.setWordWrap(True)
        head.addWidget(self.remote_status, 1)

        self.quality_box = QComboBox()
        self.quality_box.setToolTip("Качество видео")
        self.quality_box.setMinimumWidth(120)
        head.addWidget(self.quality_box, 0, Qt.AlignmentFlag.AlignVCenter)
        column.addLayout(head)

        self.translation_box = QComboBox()
        self.translation_box.setToolTip("Озвучка")
        self.translation_box.setVisible(False)
        column.addWidget(self.translation_box)

        seek_row = QHBoxLayout()
        seek_row.setSpacing(theme.GAP_S)
        self.label_pos = QLabel("0:00")
        self.label_pos.setObjectName("mono")
        self.label_pos.setMinimumWidth(48)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 1000)
        self.seek.setToolTip("Перемотка")
        self.label_dur = QLabel("0:00")
        self.label_dur.setObjectName("mono")
        self.label_dur.setMinimumWidth(48)
        seek_row.addWidget(self.label_pos)
        seek_row.addWidget(self.seek, 1)
        seek_row.addWidget(self.label_dur)
        column.addLayout(seek_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(theme.GAP_XS)
        self.btn_back = self._pad_button("back10", "−10 секунд")
        self.btn_play = self._pad_button("play", "Пуск / пауза", accent=True)
        self.btn_forward = self._pad_button("fwd10", "+10 секунд")
        self.btn_mute = self._pad_button("volume", "Звук вкл/выкл")
        self.btn_subs = self._pad_button("cc", "Субтитры")
        self.btn_full = self._pad_button("fullscreen", "Развернуть / свернуть")
        self.btn_stop = self._pad_button("stop", "Остановить и начать сначала", name="padStop")
        for button in (
            self.btn_back,
            self.btn_play,
            self.btn_forward,
            self.btn_mute,
            self.btn_subs,
            self.btn_full,
            self.btn_stop,
        ):
            buttons.addWidget(button)

        buttons.addSpacing(theme.GAP_S)
        self._build_volume_controls(buttons)
        column.addLayout(buttons)

        self._build_norm_panel(column)
        return box

    def _build_volume_controls(self, buttons) -> None:
        """Громкость и нормализация — отдельно: блок пульта не должен
        превращаться в метод на 80 строк."""
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(100)
        self.volume.setFixedWidth(88)
        self.volume.setFixedHeight(20)
        self.volume.setToolTip("Громкость")
        buttons.addWidget(self.volume, 0, Qt.AlignmentFlag.AlignVCenter)

        self.btn_norm_toggle = QPushButton()
        self.btn_norm_toggle.setObjectName("ghost")
        self.btn_norm_toggle.setCheckable(True)
        self.btn_norm_toggle.setIcon(theme.icon("normalize", 18))
        self.btn_norm_toggle.setFixedSize(32, 32)
        self.btn_norm_toggle.setToolTip("Нормализация громкости")
        buttons.addWidget(self.btn_norm_toggle, 0, Qt.AlignmentFlag.AlignVCenter)
        buttons.addStretch(1)

    def _build_norm_panel(self, column) -> None:
        """Панель нормализации: скрыта, чтобы пульт не раздувался."""
        self.norm_box = QFrame()
        norm_row = QHBoxLayout(self.norm_box)
        norm_row.setContentsMargins(0, theme.GAP_S, 0, 0)
        norm_row.setSpacing(theme.GAP_S)
        self.btn_norm = QPushButton("Выравнивать громкость")
        self.btn_norm.setObjectName("normToggle")
        self.btn_norm.setCheckable(True)
        self.btn_norm.setToolTip(
            "Подтягивать тихие источники и приглушать громкие до одного уровня"
        )
        norm_row.addWidget(self.btn_norm, 1)
        self.norm_value = QLabel("—")
        self.norm_value.setObjectName("normValue")
        norm_row.addWidget(self.norm_value, 0)
        self.norm_box.setVisible(False)
        column.addWidget(self.norm_box)

    def _pad_button(self, icon_name: str, tip: str, accent: bool = False, name: str = "pad"):
        button = QPushButton()
        button.setObjectName(name)
        if accent:
            button.setProperty("accent", "true")
        button.setIcon(theme.icon(icon_name, 22 if accent else 20))
        button.setToolTip(tip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    # -------------------------- источники --------------------------
    def _build_sources_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(theme.GAP_M, theme.GAP_M, theme.GAP_M, theme.GAP_M)
        layout.setSpacing(theme.GAP_S)

        head = QHBoxLayout()
        head.setSpacing(theme.GAP_S)
        caption = QLabel("Поддерживаемые сайты")
        caption.setObjectName("sectionTitle")
        head.addWidget(caption, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        self.btn_sources_refresh = QPushButton()
        self.btn_sources_refresh.setObjectName("ghost")
        self.btn_sources_refresh.setIcon(theme.icon("refresh", 18))
        self.btn_sources_refresh.setFixedSize(32, 32)
        self.btn_sources_refresh.setToolTip("Обновить список")
        head.addWidget(self.btn_sources_refresh)
        layout.addLayout(head)

        self.sources_table = QTableWidget(0, 3)
        self.sources_table.setObjectName("sourcesTable")
        self.sources_table.setHorizontalHeaderLabels(["Сайт", "Состояние", "Оговорка"])
        self.sources_table.verticalHeader().setVisible(False)
        self.sources_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.sources_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.sources_table.setShowGrid(False)
        header = self.sources_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.sources_table, 1)
        return page

    # ---------------------------- журнал ----------------------------
    def _build_journal_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(theme.GAP_M, theme.GAP_M, theme.GAP_M, theme.GAP_M)
        layout.setSpacing(theme.GAP_S)

        head = QHBoxLayout()
        head.setSpacing(theme.GAP_S)
        caption = QLabel("Журнал работы")
        caption.setObjectName("sectionTitle")
        head.addWidget(caption, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        self.btn_journal_copy = QPushButton()
        self.btn_journal_copy.setObjectName("ghost")
        self.btn_journal_copy.setIcon(theme.icon("copy", 18))
        self.btn_journal_copy.setFixedSize(32, 32)
        self.btn_journal_copy.setToolTip("Скопировать журнал целиком")
        head.addWidget(self.btn_journal_copy)
        layout.addLayout(head)

        self.journal = QPlainTextEdit()
        self.journal.setObjectName("journalView")
        self.journal.setReadOnly(True)
        self.journal.setMaximumBlockCount(2000)
        layout.addWidget(self.journal, 1)
        return page

    # ------------------------------------------------------------------
    #                            связи сигналов
    # ------------------------------------------------------------------
    def _connect(self) -> None:
        self.btn_open.clicked.connect(self._open)
        self.input_url.returnPressed.connect(self._open)
        self.btn_copy.clicked.connect(self._copy)
        self.btn_browser.clicked.connect(self._open_in_browser)
        self.btn_refresh.clicked.connect(self._check_proxy)
        self.btn_sources_refresh.clicked.connect(lambda: self._load_sources(force=True))
        self.btn_journal_copy.clicked.connect(
            lambda: QApplication.clipboard().setText(self.journal.toPlainText())
        )
        self.btn_norm_toggle.toggled.connect(self.norm_box.setVisible)
        self.btn_norm.toggled.connect(self._normalisation_changed)

        # пульт
        self.btn_play.clicked.connect(lambda: self._cmd("toggle"))
        self.btn_back.clicked.connect(lambda: self._cmd("seekRel", -10))
        self.btn_forward.clicked.connect(lambda: self._cmd("seekRel", 10))
        self.btn_mute.clicked.connect(lambda: self._cmd("mute", True))
        self.btn_stop.clicked.connect(lambda: self._cmd("restart"))
        self.btn_full.clicked.connect(lambda: self._cmd("fullscreen", True))
        self.btn_subs.clicked.connect(self._cycle_subtitles)
        self.volume.sliderMoved.connect(lambda v: self._cmd("volume", round(v / 100, 2)))
        self.seek.sliderMoved.connect(self._seek_moved)
        self.quality_box.currentIndexChanged.connect(self._quality_changed)
        self.translation_box.currentIndexChanged.connect(self._translation_changed)

        # ответы фоновых потоков
        self.session_ready.connect(self._on_session_ready)
        self.state_arrived.connect(self._apply_state)
        self.sources_arrived.connect(self._fill_sources)
        self.journal_arrived.connect(self._append_journal)
        self.error_text.connect(self._on_error)
        self.loudness_arrived.connect(self.norm_value.setText)

    def _restore_tab(self) -> None:
        self.tabs.setCurrentIndex(0)

    # ------------------------------------------------------------------
    #                              действия
    # ------------------------------------------------------------------
    def _open(self) -> None:
        url = self.input_url.text().strip()
        if not url:
            self.input_url.setFocus()
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        self._set_remote_status("Пульт: разбираю ссылку…")
        self._badge("warn")
        self.btn_open.setEnabled(False)

        def worker() -> None:
            try:
                result = self.api.create_session(url)
            except ApiError as exc:
                self.error_text.emit(str(exc))
                return
            finally:
                pass
            if not (result.data or {}).get("ok", True):
                self.error_text.emit(result.reason)
                return
            data = result.data or {}
            self._fill_translations(data)
            self.session_ready.emit(str(data.get("player") or ""), str(data.get("sid") or ""))

        threading.Thread(target=worker, daemon=True).start()

    def _on_session_ready(self, player_url: str, sid: str) -> None:
        self.btn_open.setEnabled(True)
        if not player_url:
            self._on_error("прокси вернул пустой адрес страницы")
            return
        self.player_url = player_url
        self.sid = sid
        # Новое видео — субтитры прошлого не переносим.
        self.subs_index = 0
        self._subtitle_titles.clear()
        self._mark_subs_button()
        self.output_url.setText(player_url)
        QApplication.clipboard().setText(player_url)
        self._set_remote_status("Пульт: ссылка скопирована — вставьте её в телевизор")
        self._badge("ok")
        self._hide_error()
        self._poll_state()

    def _fill_translations(self, data: dict) -> None:
        titles = data.get("translations") or []
        self.translation_box.blockSignals(True)
        self.translation_box.clear()
        self.translation_box.addItems([str(t) for t in titles])
        self.translation_box.blockSignals(False)
        # больше одной озвучки — показываем выбор, иначе прячем: дублей не надо
        self.translation_box.setVisible(len(titles) > 1)

    def _fill_qualities(self, qualities: list[str]) -> None:
        if not qualities:
            return
        current = self.quality_box.currentText()
        wanted = ["Авто", *qualities]
        if [self.quality_box.itemText(i) for i in range(self.quality_box.count())] == wanted:
            return
        self.quality_box.blockSignals(True)
        self.quality_box.clear()
        self.quality_box.addItems(wanted)
        index = self.quality_box.findText(current)
        self.quality_box.setCurrentIndex(max(0, index))
        self.quality_box.blockSignals(False)

    def _cmd(self, cmd: str, value=None) -> None:
        if not self.sid:
            return

        def worker() -> None:
            try:
                result = self.api.control(self.sid, cmd, value)
            except ApiError as exc:
                self.error_text.emit(str(exc))
                return
            if not result.ok:
                self.error_text.emit(result.reason)

        threading.Thread(target=worker, daemon=True).start()

    def _seek_moved(self, value: int) -> None:
        duration = self._duration()
        if duration <= 0:
            return
        self._cmd("seek", round(value / 1000 * duration))

    def _quality_changed(self, index: int) -> None:
        if index < 0 or not self.sid:
            return
        label = self.quality_box.itemText(index)
        height = 0 if label == "Авто" else int("".join(c for c in label if c.isdigit()) or 0)
        self._cmd("quality", height)

    def _translation_changed(self, index: int) -> None:
        if index < 0 or not self.sid:
            return

        def worker() -> None:
            try:
                result = self.api.translation(self.sid, index)
            except ApiError as exc:
                self.error_text.emit(str(exc))
                return
            if not result.ok:
                self.error_text.emit(result.reason)
                return
            self._set_remote_status("Пульт: озвучка сменилась — телевизор перезагрузит страницу")

        threading.Thread(target=worker, daemon=True).start()

    def _copy(self) -> None:
        if self.output_url.text():
            QApplication.clipboard().setText(self.output_url.text())

    def _open_in_browser(self) -> None:
        from PySide6.QtCore import QUrl  # noqa: PLC0415
        from PySide6.QtGui import QDesktopServices  # noqa: PLC0415

        if self.player_url:
            QDesktopServices.openUrl(QUrl(self.player_url))

    def _normalisation_changed(self, on: bool) -> None:
        self.btn_norm.setProperty("active", "true" if on else "false")
        self.norm_value.setProperty("active", "true" if on else "false")
        for widget in (self.btn_norm, self.norm_value):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self._cmd("normalize", on)
        if on and self.sid:
            self._load_loudness()

    # ------------------------------------------------------------------
    #                        опрос состояния
    # ------------------------------------------------------------------
    def _poll_state(self) -> None:
        if not self.sid:
            return

        def worker() -> None:
            try:
                self.state_arrived.emit(self.api.state(self.sid))
            except ApiError:
                self.state_arrived.emit({"unreachable": True})

        threading.Thread(target=worker, daemon=True).start()
        QTimer.singleShot(800, self._poll_state)

    def _duration(self) -> float:
        try:
            return float(getattr(self, "_duration_value", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _apply_state(self, state: dict) -> None:
        if state.get("unreachable"):
            self._set_remote_status("Пульт: прокси не отвечает")
            self._badge("error")
            return
        duration = float(state.get("duration") or 0)
        position = float(state.get("currentTime") or 0)
        self._duration_value = duration
        self.label_pos.setText(_mmss(position))
        self.label_dur.setText(_mmss(duration))
        if not self.seek.isSliderDown() and duration > 0:
            self.seek.blockSignals(True)
            self.seek.setValue(int(position / duration * 1000))
            self.seek.blockSignals(False)

        if state.get("error"):
            self._on_error(str(state["error"]))
            return
        self._fill_qualities([str(q) for q in state.get("qualities") or []])
        # Субтитры: список приходит из /state. Пока он не пришёл, кнопка
        # молча ничего не делает — поэтому обновляем его здесь же.
        self._subtitle_titles = [str(t) for t in state.get("subtitles") or []]
        if self.subs_index > len(self._subtitle_titles):
            self.subs_index = 0
        playing = bool(state.get("playing"))
        self._set_remote_status(
            f"Пульт: {'играет' if playing else 'пауза'}  {_mmss(position)} / {_mmss(duration)}"
        )
        self.btn_play.setIcon(theme.icon("pause" if playing else "play", 22))
        self.btn_mute.setIcon(theme.icon("muted" if state.get("muted") else "volume", 20))

    def _on_error(self, message: str) -> None:
        self.btn_open.setEnabled(True)
        self._set_remote_status(f"Ошибка: {message}")
        self._badge("error")
        if message != self._last_err_key:
            self._last_err_key = message
            self.journal.appendPlainText(f"ОШИБКА: {message}")

    def _hide_error(self) -> None:
        self._last_err_key = ""

    def _load_loudness(self) -> None:
        if not self.sid:
            return

        def worker() -> None:
            try:
                data = self.api.loudness(self.sid)
            except ApiError:
                self.loudness_arrived.emit("—")
                return
            gain = data.get("gainDb")
            if gain is None:
                self.loudness_arrived.emit("нет данных")
            elif abs(float(gain)) < 1:
                self.loudness_arrived.emit("в норме")
            else:
                self.loudness_arrived.emit(f"{float(gain):+.1f} дБ")

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    #                          источники и журнал
    # ------------------------------------------------------------------
    def _check_proxy(self) -> None:
        self._badge("warn")
        self.badge.setText("прокси: проверяю…")

        def worker() -> None:
            if self.api.alive():
                self.badge.setText("прокси: работает")
                self.badge.setProperty("state", "ok")
                self.badge.style().unpolish(self.badge)
                self.badge.style().polish(self.badge)
                self._load_sources(force=True)
                self._load_journal()
            else:
                self.badge.setText("прокси: выключен")
                self.badge.setProperty("state", "error")
                self.badge.style().unpolish(self.badge)
                self.badge.style().polish(self.badge)

        threading.Thread(target=worker, daemon=True).start()

    def _load_sources(self, force: bool = False) -> None:  # noqa: ARG002 — параметр для читаемости вызова
        def worker() -> None:
            try:
                self.sources_arrived.emit(self.api.sources())
            except ApiError:
                self.sources_arrived.emit([])

        threading.Thread(target=worker, daemon=True).start()

    def _fill_sources(self, sources: list[dict]) -> None:
        self.sources_table.setRowCount(len(sources))
        for row, item in enumerate(sources):
            name = str(item.get("name") or "")
            state = str(item.get("state") or "")
            note = str(item.get("note") or "")
            label, _style_name = STATE_LABELS.get(state, (state or "неизвестно", "stateWarn"))

            name_item = QTableWidgetItem(name)
            state_item = QTableWidgetItem(label)
            note_item = QTableWidgetItem(note)
            self.sources_table.setItem(row, 0, name_item)
            self.sources_table.setItem(row, 1, state_item)
            self.sources_table.setItem(row, 2, note_item)
            # цвет состояния — через objectName-совместимый стиль
            state_item.setForeground(_color_for(state))

    def _load_journal(self) -> None:
        def worker() -> None:
            with contextlib.suppress(ApiError):
                self.journal_arrived.emit(self.api.journal(400))

        threading.Thread(target=worker, daemon=True).start()

    def _append_journal(self, text: str) -> None:
        if text:
            self.journal.setPlainText(text)
            self.journal.verticalScrollBar().setValue(self.journal.verticalScrollBar().maximum())

    def _cycle_subtitles(self) -> None:
        """Переключить субтитры по кругу: выкл → дорожка 1 → 2 → … → выкл.

        Почему по кругу, а не выпадающим списком: кнопка одна, дорожек
        обычно одна-две. Список из одной строки выглядел бы абсурдно.
        Когда дорожек нет вовсе, кнопка честно об этом говорит.
        """
        count = len(self._subtitle_titles)
        if count == 0:
            self._set_remote_status("У этого видео нет субтитров")
            return
        self.subs_index = (self.subs_index + 1) % (count + 1)
        self._cmd("subs", self.subs_index)
        # Кнопка подсвечивается, когда субтитры включены: иначе непонятно,
        # сработало нажатие или нет — на пульте нет своего индикатора.
        self._mark_subs_button()
        if self.subs_index == 0:
            self._set_remote_status("Субтитры выключены")
        else:
            name = self._subtitle_titles[self.subs_index - 1]
            self._set_remote_status(f"Субтитры: {name}")

    def _mark_subs_button(self) -> None:
        """Подсветить кнопку субтитров, когда дорожка включена."""
        self.btn_subs.setProperty("active", "true" if self.subs_index else "false")
        self.btn_subs.style().unpolish(self.btn_subs)
        self.btn_subs.style().polish(self.btn_subs)

    # ------------------------------------------------------------------
    #                              мелочи
    # ------------------------------------------------------------------
    def _set_remote_status(self, text: str) -> None:
        self.remote_status.setText(text)

    def _badge(self, state: str) -> None:
        self.badge.setProperty("state", state)
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)


def _mmss(seconds: float) -> str:
    """Время в виде 12:34 — как в плеере, а не секундами."""
    total = max(0, int(seconds or 0))
    return f"{total // 60}:{total % 60:02d}"


def _color_for(state: str):
    from PySide6.QtGui import QColor  # noqa: PLC0415

    return {
        "ok": QColor("#34D399"),
        "partial": QColor("#FBBF24"),
        "needs_cookies": QColor("#FBBF24"),
        "browser_only": QColor("#FBBF24"),
        "broken": QColor("#F87171"),
    }.get(state, QColor("#CBD2E4"))


__all__ = ["TAB_JOURNAL", "TAB_PULT", "TAB_SOURCES", "Panel"]
