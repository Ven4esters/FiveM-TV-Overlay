"""Общие фикстуры для тестов.

Правило: unit-тесты не ходят в сеть и не зависят от времени (п. 7 инструкции).
Всё внешнее подменяется здесь же.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Тесты должны видеть пакет без установки: src в пути.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture()
def isolated_pins(tmp_path, monkeypatch):
    """pins с временным хранилищем — чтобы тесты не трогали videos/pins.json."""
    from tvhost.core import pins

    monkeypatch.setattr(pins, "BASE_DIR", tmp_path)
    monkeypatch.setattr(pins, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(pins, "PINS_PATH", tmp_path / "pins.json")
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    pins._pins.clear()
    pins._downloading.clear()
    pins._logs.clear()
    yield pins
    pins._pins.clear()
    pins._downloading.clear()
    pins._logs.clear()


@pytest.fixture()
def isolated_diag(tmp_path, monkeypatch):
    """diagnostics с временным файлом журнала — без записи в папку проекта."""
    from tvhost.infra import diagnostics

    log_path = tmp_path / "diag.log"
    monkeypatch.setattr(diagnostics, "LOG_PATH", str(log_path))
    diagnostics.clear()
    yield diagnostics
    diagnostics.clear()
