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
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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
from ..core import registry
from ..core.models import episode_index
from ..sources import register_all
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
    error_text = Signal(str, str)  # (что случилось, что делать)
    loudness_arrived = Signal(str)
    episode_ready = Signal()
    """Серия сменилась: плееру в игре надо перезагрузить страницу."""
    history_ready = Signal(list)  # список недавних просмотров

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
        self._muted = False
        """Звук выключен? Кнопка обязана ПЕРЕКЛЮЧАТЬ, а не только выключать.

        Раньше здесь стояло `self._cmd("mute", True)` — то есть каждый клик
        отправлял «выключи», и включить обратно с пульта было нечем. Это и
        была жалоба «звук убирается, а вернуть его нельзя».
        """
        self._fullscreen = False
        """Развёрнуто ли изображение. Кнопка тоже переключает, а не только
        разворачивает: раньше она всегда слала `fullscreen=True`."""
        self._error_shown = False
        self._hint_text = ""
        self._tabs_built = False

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setWindowIcon(theme.app_icon())
        # Размеры. Раньше задан был только минимум по ширине, и окно
        # «прыгало»: список недавних фиксирует высоту по числу записей,
        # подсказка об ошибке просит высоту под текст, а журнал тянется за
        # содержимым. В итоге окно то растягивалось вверх, то становилось
        # шире. Теперь есть стартовый размер и ПОТОЛОК: ниже и выше него
        # окно не полезет, что бы ни происходило внутри.
        self.setMinimumSize(520, 460)
        self.resize(720, 620)

        self._build()
        self._connect()
        self._restore_tab()
        self._load_history()
        # Проверить прокси при старте. Раньше `_check_proxy` висел ТОЛЬКО на
        # кнопке «обновить», и до первого нажатия в шапке светилось исходное
        # «прокси: проверяю…», а вкладка «Источники» оставалась пустой —
        # выглядело как «функции не работают», хотя прокси жив.
        # Задержка нужна из-за режима --serve: сервер поднимается в этом же
        # процессе, и окно успевает построиться раньше первого ответа.
        QTimer.singleShot(STARTUP_PROXY_CHECK_MS, self._check_proxy)
        # QSS ставится на ПРИЛОЖЕНИЕ, а не на это окно: стиль на конкретном
        # виджете конфликтует с application-стилем, и Qt молча применяет
        # только часть правил (кнопка #primary оставалась системной серой).
        # Одна точка применения — application.setStyleSheet() при старте.

    # ------------------------------------------------------------------
    #                              построение
    # ------------------------------------------------------------------
    def _build(self) -> None:
        # Потолок высоты самой карточки: содержимое (журнал, подсказка)
        # больше не может вытянуть окно за экран.
        self.setMaximumHeight(900)

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
        self.history_box = self._build_history_block()
        layout.addWidget(self.history_box)
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

        # Список НЕ переписываем руками: он брался из головы и отставал от
        # реестра (появились anidub.org, yummyani, ani-media). Теперь это
        # факт из реестра — тот же, что отдаёт ошибка «не поддерживается».
        hint = QLabel(_supported_preview())
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

    history_list_limit = 5
    """Сколько недавних показываем. Совпадает с пределом истории на сервере."""

    def _build_history_block(self) -> QWidget:
        """Последние пять просмотров: вернуться к тому, что играло.

        Свёрнут, пока истории нет: пустая карточка на пульте — это шум,
        а не польза. Появляется сама, как только первое видео открылось.
        """
        box = QFrame()
        box.setObjectName("card")
        column = QVBoxLayout(box)
        column.setContentsMargins(theme.GAP_M, theme.GAP_M, theme.GAP_M, theme.GAP_M)
        column.setSpacing(theme.GAP_S)

        head = QHBoxLayout()
        head.setSpacing(theme.GAP_S)
        caption = QLabel("Недавние")
        caption.setObjectName("sectionTitle")
        head.addWidget(caption, 0, Qt.AlignmentFlag.AlignVCenter)
        self.history_hint = QLabel("вернуться к тому, что играло")
        self.history_hint.setObjectName("faint")
        head.addWidget(self.history_hint, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        self.btn_history_clear = QPushButton()
        self.btn_history_clear.setObjectName("ghost")
        self.btn_history_clear.setIcon(theme.icon("close", 16))
        self.btn_history_clear.setFixedSize(24, 24)
        self.btn_history_clear.setToolTip("Очистить недавние")
        self.btn_history_clear.setVisible(False)
        head.addWidget(self.btn_history_clear, 0, Qt.AlignmentFlag.AlignVCenter)
        column.addLayout(head)

        self.history_list = QListWidget()
        self.history_list.setObjectName("historyList")
        # Высота считается из строк, а не задаётся на глаз. Две строки текста
        # (название + подпись) при 14/12 px и отступах 8 px дают ~44 px на
        # запись; первые пять записей должны быть видны без прокрутки, иначе
        # смысл «вернуться» теряется — люди не догадываются листать.
        # Размер по содержимому: QListWidget сам скажет, сколько ему нужно на
        # все пять записей. Числа «на глаз» не работают — шрифт и отступы
        # в QSS меняются, и список обрезал записи.
        self.history_list.setSizeAdjustPolicy(QAbstractItemView.SizeAdjustPolicy.AdjustToContents)
        self.history_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.history_list.setSpacing(2)
        column.addWidget(self.history_list)

        box.setVisible(False)
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

    def _build_hint_row(self) -> QHBoxLayout:
        """Подсказка «что делать» под ошибкой.

        Отдельной строкой, потому что в одну строку с причиной она не
        влезает и обрезается многоточием — а смысл подсказки в том, чтобы её
        ПРОЧИТАЛИ. Скрыта, пока всё в порядке: постоянная плашка «на случай
        ошибки» приучает не смотреть в это место.
        """
        self.hint_label = QLabel("")
        self.hint_label.setObjectName("hintText")
        self.hint_label.setWordWrap(True)
        self.hint_label.setVisible(False)

        self.btn_hint_close = QPushButton()
        self.btn_hint_close.setObjectName("ghost")
        self.btn_hint_close.setIcon(theme.icon("close", 14))
        self.btn_hint_close.setFixedSize(20, 20)
        self.btn_hint_close.setToolTip("Скрыть подсказку")
        self.btn_hint_close.setVisible(False)
        self.btn_hint_close.clicked.connect(self._hide_error)

        row = QHBoxLayout()
        row.setSpacing(theme.GAP_S)
        row.addWidget(self.hint_label, 1)
        # Крестик по верхнему краю: на трёхстрочной подсказке он должен
        # стоять у первой строки, а не висеть по центру блока.
        row.addWidget(self.btn_hint_close, 0, Qt.AlignmentFlag.AlignTop)
        return row

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

        column.addLayout(self._build_hint_row())
        column.addLayout(self._build_picks_row())

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

    def _build_picks_row(self) -> QHBoxLayout:
        """Озвучка и серия — в одной строке.

        Два ответа на один вопрос «что именно играем», поэтому и стоят
        рядом. Оба скрыты, пока сервер не сказал, что выбирать есть из
        чего: пустой список озвучек или серий только путает.
        """
        picks = QHBoxLayout()
        picks.setSpacing(theme.GAP_S)
        self.translation_box = QComboBox()
        self.translation_box.setToolTip("Озвучка")
        self.translation_box.setVisible(False)
        picks.addWidget(self.translation_box, 1)

        self.episode_box = QComboBox()
        self.episode_box.setToolTip("Серия")
        self.episode_box.setVisible(False)
        picks.addWidget(self.episode_box, 0)
        return picks

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
        self.history_ready.connect(self._fill_history)
        self.history_list.itemClicked.connect(self._history_clicked)
        self.btn_history_clear.clicked.connect(self._clear_history)
        self.btn_norm.toggled.connect(self._normalisation_changed)

        # пульт
        self.btn_play.clicked.connect(lambda: self._cmd("toggle"))
        self.btn_back.clicked.connect(lambda: self._cmd("seekRel", -10))
        self.btn_forward.clicked.connect(lambda: self._cmd("seekRel", 10))
        self.btn_mute.clicked.connect(self._toggle_mute)
        self.btn_stop.clicked.connect(lambda: self._cmd("restart"))
        self.btn_full.clicked.connect(self._toggle_fullscreen)
        self.btn_subs.clicked.connect(self._cycle_subtitles)
        self.volume.sliderMoved.connect(lambda v: self._cmd("volume", round(v / 100, 2)))
        self.seek.sliderMoved.connect(self._seek_moved)
        self.quality_box.currentIndexChanged.connect(self._quality_changed)
        self.translation_box.currentIndexChanged.connect(self._translation_changed)
        self.episode_box.activated.connect(self._episode_picked)

        # ответы фоновых потоков
        self.session_ready.connect(self._on_session_ready)
        self.state_arrived.connect(self._apply_state)
        self.sources_arrived.connect(self._fill_sources)
        self.journal_arrived.connect(self._append_journal)
        self.error_text.connect(self._on_error)
        self.loudness_arrived.connect(self.norm_value.setText)
        self.episode_ready.connect(self._after_episode)

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
                self.error_text.emit(str(exc), "Проверьте, что прокси запущен, и повторите")
                return
            finally:
                pass
            if not (result.data or {}).get("ok", True):
                self.error_text.emit(result.reason, result.hint)
                return
            data = result.data or {}
            self._fill_translations(data)
            self._fill_episodes(data)
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
        self._load_history()
        # Нормализация: замер нужен только когда она включена, но если
        # включена — мерить надо СРАЗУ, а не после повторного клика.
        # Раньше замер запускался только в обработчике тумблера, поэтому
        # при уже включённой нормализации на новом видео висело «нет данных».
        if self.btn_norm.isChecked():
            self._load_loudness()

    def _load_history(self) -> None:
        """Перечитать список недавних. Вызывается после каждого открытия."""

        def worker() -> None:
            try:
                items = self.api.history()
            except ApiError:
                return
            self.history_ready.emit(items)

        threading.Thread(target=worker, daemon=True).start()

    def _fill_history(self, items: list[dict]) -> None:
        """Нарисовать недавние: название, источник и когда.

        Правила проекта: иконка вместо символа, цвета из палитры темы,
        подписи 12 px серым. Название обрезается многоточием — ссылки
        длинные, а переносить строку в списке значит терять ритм.
        """
        self.history_list.clear()
        for item in items:
            title = str(item.get("title") or item.get("url") or "без названия")
            source = str(item.get("source") or "")
            when = str(item.get("when") or "")
            translation = str(item.get("translation") or "")
            subtitle = " · ".join(part for part in (source, translation, when) if part)
            entry = QListWidgetItem()
            entry.setText(f"{title}\n{subtitle}" if subtitle else title)
            entry.setIcon(theme.icon("film", 16))
            entry.setToolTip(f"{title}\n{item.get('url', '')}")
            entry.setData(Qt.ItemDataRole.UserRole, str(item.get("url") or ""))
            self.history_list.addItem(entry)
        # Высота под все записи: считаем по фактическим строкам, а не
        # константой. sizeHintForRow честно учитывает шрифт и отступы QSS,
        # а ручные «44 px» давали обрезку на другой машине.
        rows = self.history_list.count()
        if rows:
            # Шаг строки = высота строки + отступ между строками (setSpacing).
            # Без второго слагаемого последняя запись обрезается на 4 px —
            # проверено замером visualItemRect, а не на глаз.
            step = (self.history_list.sizeHintForRow(0) or 44) + self.history_list.spacing() * 2
            frame = 2 * self.history_list.frameWidth()
            visible = min(rows, self.history_list_limit)
            # Не выше 40 % окна: пять длинных названий иначе вытягивали
            # окно так, что нижние кнопки уходили за край экрана.
            ceiling = max(0, int(self.height() * 0.4))
            wanted = step * visible + frame
            self.history_list.setFixedHeight(min(wanted, ceiling) if ceiling else wanted)
        has_items = bool(items)
        self.history_box.setVisible(has_items)
        self.btn_history_clear.setVisible(has_items)

    def _clear_history(self) -> None:
        """Убрать все недавние. Подтверждения нет: они в памяти и вернутся
        сами, если снова открыть видео — терять нечего."""
        self.history_list.clear()
        self.history_box.setVisible(False)
        self.btn_history_clear.setVisible(False)

    def _history_clicked(self, entry: QListWidgetItem) -> None:
        """Клик по недавнему: подставить ссылку и открыть заново."""
        url = str(entry.data(Qt.ItemDataRole.UserRole) or "")
        if not url:
            return
        self.input_url.setText(url)
        self._open()

    def _fill_translations(self, data: dict) -> None:
        titles = data.get("translations") or []
        self.translation_box.blockSignals(True)
        self.translation_box.clear()
        self.translation_box.addItems([str(t) for t in titles])
        self.translation_box.blockSignals(False)
        # больше одной озвучки — показываем выбор, иначе прячем: дублей не надо
        self.translation_box.setVisible(len(titles) > 1)

    def _fill_episodes(self, data: dict) -> None:
        """Нарисовать выбор серии. Скрыт, если сериал не опознан.

        Данные приходят из `/new` и `/state`: список серий и номер текущей.
        У фильмов и у источников без списков (Rutube, YouTube) там пусто —
        тогда переключатель не показываем вовсе, а не показываем пустой.
        """
        titles = [str(t) for t in data.get("episodes") or []]
        # Показываем выбор, только если ИСТОЧНИК умеет переключать серии
        # (сервер отвечает `canSwitchEpisode`). Один список ни о чём не
        # говорит: у anidub.org серия приходит параметром, у Kodik — новым
        # потоком, а у фильма со «связанными» видео список может быть, хотя
        # переключать нечего.
        can = data.get("canSwitchEpisode")
        if can is None:
            can = len(titles) > 1
        self.episode_box.blockSignals(True)
        self.episode_box.clear()
        if titles:
            self.episode_box.addItems(titles)
            # Номер может прийти строкой («3») — приводим, но без падения:
            # список серий не должен ломать пульт.
            try:
                current = int(data.get("episode") or 0)
            except (TypeError, ValueError):
                current = 0
            # Ставим по НОМЕРУ серии, а не по «номер минус один». У сериала
            # бывают дыры в нумерации (у anidub.org встречается), и тогда
            # индекс в списке не совпадает с номером: при сериях 1, 3, 5
            # третья строка — это 5-я серия, а не 3-я. Номер серии есть в
            # подписи («3 серия»), по ней и сверяемся.
            #
            # -1 значит «такой серии в списке нет» — оставляем как выбрал
            # Qt (первую строку), а не ставим мусорный индекс.
            found = episode_index(titles, current)
            if found >= 0:
                self.episode_box.setCurrentIndex(found)
            season = str(data.get("episodeSeason") or "").strip()
            self.episode_box.setToolTip(f"{season} сезон · серия" if season else "Серия")
        self.episode_box.blockSignals(False)
        self.episode_box.setVisible(bool(can))

    def _episode_picked(self, index: int) -> None:
        """Выбрали серию: просим сервер разобрать её и открыть заново.

        Почему отдельным запросом, а не командой плееру: у Kodik каждая
        серия — свой поток со своими `id`/`hash`, «переключить дорожку»
        физически нечего. Сервер разбирает ссылку заново, плеер в игре
        перезагружает страницу и играет уже нужную серию.
        """
        if not self.sid or index < 0:
            return
        number = index + 1
        self._set_remote_status(f"Пульт: открываю {number}-ю серию…")

        def worker() -> None:
            try:
                result = self.api.episode(self.sid, number)
            except ApiError as exc:
                self.error_text.emit(str(exc), "Проверьте, что прокси запущен")
                return
            if not result.ok:
                self.error_text.emit(result.reason, result.hint)
                return
            self.episode_ready.emit()

        threading.Thread(target=worker, daemon=True).start()

    def _after_episode(self) -> None:
        """Серия сменилась: обновить картинку и напомнить про плеер в игре.

        Страницу плеера в игре надо перезагрузить: это отдельное окно
        браузера, и само оно про смену серии не узнает.
        """
        self._set_remote_status("Пульт: серия открыта — перезагрузите телевизор (F5)")
        self._poll_state()

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
                self.error_text.emit(str(exc), "Проверьте, что прокси запущен")
                return
            if not result.ok:
                self.error_text.emit(result.reason, result.hint)

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
                self.error_text.emit(str(exc), "Проверьте, что прокси запущен")
                return
            if not result.ok:
                self.error_text.emit(result.reason, result.hint)
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
        self.norm_value.setText("меряю…" if on else "—")
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
            # Не затираем отказ: «прокси не отвечает» — не новость, когда
            # человек только что прочитал, почему ссылка не открылась.
            self._set_remote_status("Пульт: прокси не отвечает")
            if self._error_shown:
                return
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
        # Серии тоже приходят в /state: после переключения номера список
        # должен стоять на новой серии, иначе выбор «отскакивает».
        if state.get("episodes") or "canSwitchEpisode" in state:
            self._fill_episodes(state)
        if self.subs_index > len(self._subtitle_titles):
            self.subs_index = 0
        playing = bool(state.get("playing"))
        self._set_remote_status(
            f"Пульт: {'играет' if playing else 'пауза'}  {_mmss(position)} / {_mmss(duration)}"
        )
        self.btn_play.setIcon(theme.icon("pause" if playing else "play", 22))
        self.btn_mute.setIcon(theme.icon("muted" if state.get("muted") else "volume", 20))

    def _on_error(self, message: str, hint: str = "") -> None:
        """Показать отказ так, чтобы его нельзя было не заметить.

        Почему не просто `setText`: опрос пульта идёт каждые 800 мс, и
        `_apply_state` перезаписывает строку статуса словами «играет
        0:00 / 0:00». Подсказка «сайт не поддерживается» исчезала раньше,
        чем человек успевал дочитать — именно на это была жалоба. Поэтому
        отказ помечается флагом: опрос его не трогает, а снимает только
        новое открытие или ручное закрытие.
        """
        self.btn_open.setEnabled(True)
        self._error_shown = True
        # force: сам отказ должен пробить защиту, которую он же и включает.
        self._set_remote_status(f"Ошибка: {message}", force=True)
        self.remote_status.setObjectName("errorText")
        self.remote_status.style().unpolish(self.remote_status)
        self.remote_status.style().polish(self.remote_status)
        self._hint_text = hint
        self.hint_label.setText(hint)
        self.hint_label.setVisible(bool(hint))
        if hint:
            # QLabel с переносом слов не пересчитывает высоту сам: текст
            # обрезался на третьей строке — видно на скриншоте проверки.
            # Ширину знает layout, поэтому просим её у него и только потом
            # фиксируем высоту под фактический текст.
            self.hint_label.setMinimumHeight(0)
            width = max(self.hint_label.width(), 320)
            needed = self.hint_label.heightForWidth(width)
            if needed > 0:
                # Потолок: длинная подсказка не должна раздвигать окно.
                # Лучше многоточие в двух строках, чем «прыжок» на пол-экрана.
                self.hint_label.setMinimumHeight(min(needed, 96) + theme.GAP_S)
        self.btn_hint_close.setVisible(True)
        self._badge("error")
        if message != self._last_err_key:
            self._last_err_key = message
            self.journal.appendPlainText(f"ОШИБКА: {message}")

    def _hide_error(self) -> None:
        """Снять отказ: вызывается при новом открытии и кнопкой закрытия."""
        self._last_err_key = ""
        self._error_shown = False
        self._hint_text = ""
        self._error_shown = False
        self._hint_text = ""
        self.hint_label.setVisible(False)
        self.btn_hint_close.setVisible(False)
        self.remote_status.setObjectName("faint")
        self.remote_status.style().unpolish(self.remote_status)
        self.remote_status.style().polish(self.remote_status)

    def _load_loudness(self) -> None:
        if not self.sid:
            return

        def worker() -> None:
            try:
                data = self.api.loudness(self.sid)
            except ApiError:
                self.loudness_arrived.emit("не удалось замерить")
                return
            gain = data.get("gainDb")
            if gain is None:
                # Сервер объясняет причину в `note` («ffmpeg не найден»,
                # «замер не удался»). Показываем ЕЁ: «нет данных» ничего не
                # говорит человеку и выглядит как поломка кнопки.
                self.loudness_arrived.emit(str(data.get("note") or "замер не удался")[:34])
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
            else:
                self.badge.setText("прокси: выключен")
                self.badge.setProperty("state", "error")
                self.badge.style().unpolish(self.badge)
                self.badge.style().polish(self.badge)
            # Источники и журнал грузим В ЛЮБОМ СЛУЧАЕ. Раньше они висели
            # внутри ветки «alive() == True»: одна неудачная проверка — и
            # вкладка «Источники» оставалась пустой навсегда, хотя прокси мог
            # ответить уже через секунду. Список источников — статический, он
            # не зависит от того, ответил ли health-check прямо сейчас.
            self._load_sources(force=True)
            self._load_journal()

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

    def _toggle_mute(self) -> None:
        """Звук вкл/выкл. Состояние держим у себя: у пульта нет своего датчика.

        Почему не спрашивать у сервера каждый раз: опрос идёт раз в 800 мс,
        а нажатие — сразу. Если ждать ответа, второй клик придёт раньше, чем
        обновится состояние, и человек «не сможет включить обратно» даже с
        исправной кнопкой.
        """
        self._muted = not self._muted
        self._cmd("mute", self._muted)
        self._paint_mute()
        self._set_remote_status("Звук выключен" if self._muted else "Звук включён")

    def _paint_mute(self) -> None:
        """Иконка и подсветка кнопки звука по нашему состоянию."""
        self.btn_mute.setIcon(theme.icon("muted" if self._muted else "volume", 20))
        self.btn_mute.setProperty("active", "false" if self._muted else "true")
        self.btn_mute.style().unpolish(self.btn_mute)
        self.btn_mute.style().polish(self.btn_mute)

    def _toggle_fullscreen(self) -> None:
        """Развернуть / свернуть изображение в игре.

        Важно понимать, что именно разворачивается: не окно пульта, а
        `<video>` на странице плеера, которую открывает игра. Поэтому
        команда уходит в сессию, а не в Qt.
        """
        self._fullscreen = not self._fullscreen
        self._cmd("fullscreen", self._fullscreen)
        self._set_remote_status("Развёрнуто" if self._fullscreen else "Свёрнуто")

    def _cycle_subtitles(self) -> None:
        """Переключить субтитры по кругу: выкл → дорожка 1 → 2 → … → выкл.

        Почему по кругу, а не выпадающим списком: кнопка одна, дорожек
        обычно одна-две. Список из одной строки выглядел бы абсурдно.
        Когда дорожек нет вовсе, кнопка честно об этом говорит.
        """
        count = len(self._subtitle_titles)
        if count == 0:
            # Объясняем причину, а не просто «нет»: у Kodik субтитры — это
            # не дорожка `<track>`, а отдельная ОЗВУЧКА с типом
            # `data-translation-type="subtitles"`. Она приходит в списке
            # озвучек, и включать её надо там — кнопка CC тут ни при чём.
            hint = "У этого видео нет дорожек субтитров"
            if self.translation_box.count() > 0:
                hint += " — ищите их в списке озвучек"
            self._set_remote_status(hint)
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
    def set_remote_status(self, text: str) -> None:
        """Показать сообщение в строке статуса — публично, для точки входа.

        Нужен `ui/__main__.py`: если прокси не поднялся (порт занят), окно
        всё равно открывается и обязано сказать об этом человеку. В сборке
        `console=False` больше сказать негде. Сообщение о старте важнее
        случайного отказа, поэтому показываем его принудительно.
        """
        self._set_remote_status(text, force=True)

    def _set_remote_status(self, text: str, force: bool = False) -> None:
        """Строка статуса. Пока висит отказ, обычные сообщения не затирают его.

        `force=True` — для самого отказа и для явной очистки: иначе статус
        нельзя было бы сменить вообще.
        """
        if self._error_shown and not force:
            return
        self.remote_status.setText(text)

    def _badge(self, state: str) -> None:
        self.badge.setProperty("state", state)
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)


SHORT_LIST = 5
"""Сколько имён влезает в подпись под полем ссылки, прежде чем она начнёт
переноситься на вторую строку и толкать поле ввода вниз."""

STARTUP_PROXY_CHECK_MS = 400
"""Через сколько миллисекунд после постройки окна проверить прокси.

Не ноль: в режиме --serve сервер стартует в этом же процессе, и мгновенная
проверка успевает случиться раньше, чем он начнёт принимать соединения.
Полсекунды незаметны глазу и убирают эту гонку."""


def _supported_preview() -> str:
    """Короткий список источников для подписи под полем ссылки.

    Полный список длинный и в подпись не влезает, поэтому берём первые
    источники реестра и добавляем «и ещё N». Порядок реестра = порядок
    проверки, и он же приоритет — так подпись не врёт о главном.
    """
    try:
        register_all()
        names = registry.names()
    except Exception:  # реестр недоступен — подпись не критична
        return "аниме и видео по ссылке из браузера"
    if len(names) <= SHORT_LIST:
        return ", ".join(names)
    return ", ".join(names[:4]) + f" и ещё {len(names) - 4}"


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
