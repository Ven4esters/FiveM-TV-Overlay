"""Интерфейс не должен «съезжать»: виджеты не перекрывают друг друга.

Отчёт со скриншотом 2.1.4: после нажатия «Открыть на ТВ» кнопка наехала на
поле ввода, а селектор качества («Авто») перекрыл время справа.

Причина не в стилях. На пульте четыре карточки, и после первого открытия
видео появляется пятая — «Недавние». Сумма высот переставала влезать в
окно, а `addStretch` в конце не давал Qt сжать виджеты по-хорошему: он
давил их НИЖЕ минимальных высот. Замерено до исправления:

    до   открытия: низ поля 213, верх кнопки 222 → зазор  +9 px
    после открытия: низ поля 199, верх кнопки 185 → наезд −14 px

Лечение: содержимое пульта в `QScrollArea` (нехватку места забирает полоса
прокрутки) плюс явные минимальные высоты у элементов, которые сжимались.

Тесты измеряют ГЕОМЕТРИЮ настоящих виджетов, а не наличие строк в коде:
проверка «есть ли QScrollArea» осталась бы зелёной, даже если раскладка
снова поедет. Qt запускается в режиме offscreen — экран не нужен.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conftest import qt_widgets  # noqa: E402 — до импорта Qt нужен режим offscreen

QApplication = qt_widgets()


@pytest.fixture(scope="module")
def app() -> object:
    """Одно QApplication на модуль: второй экземпляр Qt не переживает."""
    return QApplication.instance() or QApplication([])


def _make_panel(app: object):
    """Пульт с темой, как в работе. Сеть не нужна: адрес заведомо мёртвый."""
    from tvhost2.ui import theme
    from tvhost2.ui.api import Api
    from tvhost2.ui.panel import Panel

    app.setStyleSheet(theme.stylesheet())  # type: ignore[attr-defined]
    return Panel(api=Api(base="http://127.0.0.1:9")), theme


def _rect(panel, widget):
    """Прямоугольник виджета в координатах окна."""
    from PySide6.QtCore import QRect

    return QRect(widget.mapTo(panel, widget.rect().topLeft()), widget.size())


def _overlaps(panel, a, b) -> bool:
    return _rect(panel, a).intersects(_rect(panel, b))


LONG_ENTRY = {
    "title": "НОВЫЕ РУССКИЕ ПЕСНИ 2026 #100 Хиты Музыка 2026 New Russian Popular Songs",
    "source": "Rutube",
    "when": "только что",
    "url": "https://rutube.ru/video/5ac078118254052f738a2a9d87b7b8a7/",
}


class TestNoWidgetOverlap:
    @pytest.mark.parametrize("size", [(720, 620), (520, 460), (620, 620), (900, 900)])
    def test_no_overlap_without_history(self, app, size) -> None:
        """До первого открытия карточки «Недавние» нет — наездов быть не должно."""
        panel, _ = _make_panel(app)
        panel.resize(*size)
        panel.show()
        app.processEvents()  # type: ignore[attr-defined]
        try:
            assert not _overlaps(panel, panel.input_url, panel.btn_open)
            assert not _overlaps(panel, panel.quality_box, panel.label_dur)
        finally:
            panel.close()

    @pytest.mark.parametrize("size", [(720, 620), (520, 460), (620, 620), (900, 900)])
    def test_no_overlap_after_history_appears(self, app, size) -> None:
        """Главный случай из отчёта: «Недавние» появились — раскладка стоит.

        Именно после этого шага кнопка наезжала на поле ввода на скриншоте.
        """
        panel, _ = _make_panel(app)
        panel.resize(*size)
        panel.show()
        panel._fill_history([LONG_ENTRY])  # noqa: SLF001 — воспроизводим состояние
        app.processEvents()  # type: ignore[attr-defined]
        try:
            assert panel.history_box.isVisible(), "карточка «Недавние» не появилась"
            assert not _overlaps(panel, panel.input_url, panel.btn_open), (
                "кнопка «Открыть на ТВ» наехала на поле ввода"
            )
            assert not _overlaps(panel, panel.quality_box, panel.label_dur), (
                "селектор качества перекрыл время"
            )
            assert not _overlaps(panel, panel.remote_status, panel.quality_box), (
                "статус пульта перекрыл селектор качества"
            )
        finally:
            panel.close()

    def test_widgets_keep_minimal_height(self, app) -> None:
        """Виджеты не сжимаются ниже минимума даже в тесном окне.

        Это корень проблемы: Qt давил поле ввода и кнопку ниже разумной
        высоты, и они визуально склеивались.
        """
        panel, _ = _make_panel(app)
        panel.resize(520, 460)
        panel.show()
        panel._fill_history([LONG_ENTRY])  # noqa: SLF001
        app.processEvents()  # type: ignore[attr-defined]
        try:
            assert panel.btn_open.height() >= panel.btn_open.minimumHeight()
            assert panel.input_url.height() >= panel.input_url.minimumHeight()
            assert panel.quality_box.height() >= panel.quality_box.minimumHeight()
        finally:
            panel.close()

    def test_content_is_scrollable_when_cramped(self, app) -> None:
        """В тесном окне появляется прокрутка — место не «съедает» виджеты."""
        panel, _ = _make_panel(app)
        panel.resize(520, 460)
        panel.show()
        panel._fill_history([LONG_ENTRY, LONG_ENTRY])  # noqa: SLF001
        app.processEvents()  # type: ignore[attr-defined]
        try:
            scroll = panel.remote_scroll.verticalScrollBar()
            assert scroll.maximum() > 0, "в тесном окне нет прокрутки — содержимое сжато"
        finally:
            panel.close()


class TestScrollAreaRules:
    def test_horizontal_scrollbar_never_appears(self, app) -> None:
        """По горизонтали прокрутки нет: ширина окна — это ширина карточек."""
        from PySide6.QtCore import Qt

        panel, _ = _make_panel(app)
        try:
            assert (
                panel.remote_scroll.horizontalScrollBarPolicy()
                == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
        finally:
            panel.close()

    def test_layout_rule_no_setgeometry(self) -> None:
        """Правило проекта: раскладка только через layouts.

        `setGeometry`/`move` в пульте означали бы ручные координаты, которые
        ломаются при другом шрифте или масштабе Windows.
        """
        from pathlib import Path

        source = Path(__file__).resolve().parents[2] / "src" / "tvhost2" / "ui" / "panel.py"
        text = source.read_text(encoding="utf-8")
        assert ".setGeometry(" not in text
        assert ".move(" not in text
