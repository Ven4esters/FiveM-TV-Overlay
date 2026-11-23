"""Панель «Нормализация громкости» в оверлее.

Проверяем не только наличие виджетов, но и главное правило дизайна:
окно НЕ раздувается — после сворачивания панели высота возвращается
ровно к прежней. Обе крайности (осталось раздутым / схлопнулось ниже)
были пойманы живьём, поэтому тест держит обе.
"""

from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv[:1])


@pytest.fixture
def overlay(app):
    from tvhost.ui.overlay import Overlay

    w = Overlay()
    w.show()
    for _ in range(3):
        app.processEvents()
    yield w
    w.close()


def test_panel_exists_and_hidden(overlay) -> None:
    assert hasattr(overlay, "norm_box")
    assert not overlay.norm_box.isVisible()


def test_toggle_button_reveals_panel(overlay, app) -> None:
    overlay.btn_norm.setChecked(True)
    for _ in range(6):
        app.processEvents()
    assert overlay.norm_box.isVisible()


def test_height_returns_after_collapse(overlay, app) -> None:
    """Правило «без раздувания»: высота возвращается точно."""
    for _ in range(3):
        app.processEvents()
    before = overlay.height()

    overlay.btn_norm.setChecked(True)
    for _ in range(6):
        app.processEvents()
    opened = overlay.height()
    assert opened > before, "панель должна добавить высоту"

    overlay.btn_norm.setChecked(False)
    for _ in range(6):
        app.processEvents()
    assert abs(overlay.height() - before) <= 4, (
        f"окно не вернулось к исходной высоте: {before} → {overlay.height()}"
    )


def test_switch_toggles_state(overlay, app) -> None:
    overlay.btn_norm_switch.setChecked(True)
    app.processEvents()
    assert overlay.btn_norm_switch.property("active") == "true"
    overlay.btn_norm_switch.setChecked(False)
    app.processEvents()
    assert overlay.btn_norm_switch.property("active") == "false"


def test_switch_is_saved_between_runs(overlay, app, tmp_path, monkeypatch) -> None:
    """Настройка запоминается — иначе включать её каждый запуск."""
    from tvhost.ui import overlay as overlay_mod

    monkeypatch.setattr(overlay_mod, "_CACHE_DIR", tmp_path)
    overlay.btn_norm_switch.setChecked(True)
    app.processEvents()
    assert (tmp_path / "ui.json").exists()
    assert overlay._load_ui_flag("normalize", False) is True
    overlay.btn_norm_switch.setChecked(False)


def test_no_player_sid_does_not_crash(overlay) -> None:
    """Панель восстанавливается до создания сессии — падать нельзя."""
    overlay.player_sid = ""
    overlay._refresh_loudness()  # без исключения
    assert overlay.lbl_norm_value.text() == "—"
