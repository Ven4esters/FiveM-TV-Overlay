"""Регрессии интерфейса v2: три бага, найденные по скриншотам.

Каждый тест закрывает ошибку, которая внешне выглядела безобидно, но ломала
работу пульта. Без этих тестов баги вернутся при первой же правке стилей или
маршрутов — именно так они и появились.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PySide6 = pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def app() -> object:
    """Одно QApplication на модуль: второй экземпляр Qt не переживает."""
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_stylesheet_paths_are_resolved() -> None:
    """QSS не должен содержать плейсхолдер @ICONS@.

    QSS не умеет относительных путей к картинкам, поэтому в файле лежит
    @ICONS@. Если его не подставить, Qt не разбирает таблицу стилей ЦЕЛИКОМ
    и молча отбрасывает ВСЕ правила: кнопки остаются системно-серыми, хотя
    сам файл синтаксически верный. Именно так и было до этой правки.
    """
    from tvhost2.ui import theme

    text = theme.stylesheet()
    assert text, "style.qss не прочитался"
    assert "@ICONS@" not in text, "плейсхолдер @ICONS@ остался — стили не применятся"
    assert str(theme.ICONS).replace("\\", "/") in text


def test_stylesheet_actually_applies(app: object) -> None:
    """Проверка не по тексту, а по картинке: кнопка #primary должна быть синей.

    Стиль может быть валидным и при этом не применяться. Единственный
    надёжный способ это поймать — отрисовать кнопку и посмотреть цвет.
    """
    from PySide6.QtWidgets import QPushButton

    from tvhost2.ui import theme

    app.setStyleSheet(theme.stylesheet())  # type: ignore[attr-defined]
    button = QPushButton("Проверка")
    button.setObjectName("primary")
    button.resize(120, 34)
    image = button.grab().toImage()

    blue = 0
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            colour = image.pixelColor(x, y)
            if colour.blue() > 200 and colour.red() < 140:
                blue += 1
    assert blue > 50, f"#primary не покрасился: синих точек {blue}"


def test_icon_renders_non_empty(app: object) -> None:
    """SVG-иконка должна превращаться в пиксели, а не в пустой квадрат."""
    from tvhost2.ui import theme

    icon = theme.icon("play", 24)
    assert not icon.isNull()
    pixmap = icon.pixmap(24, 24)
    assert not pixmap.isNull()
    assert pixmap.size().width() == 24


def test_control_uses_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """Команда пульта уходит POST-запросом.

    Сервер принимает /control только POST: на GET отвечает 404 «нет маршрута»,
    и пульт выглядел нерабочим — кнопки нажимались, но ничего не происходило.
    """
    from tvhost2.ui import api as api_module

    seen: list[tuple[str, str]] = []

    def fake_request(  # noqa: ANN001, ANN202
        self, path, params=None, *, method="GET", body=None, timeout=None
    ):
        seen.append((path, method))
        return api_module.ApiResult(ok=True, status=200, data={"ok": True})

    monkeypatch.setattr(api_module.Api, "_request", fake_request)
    client = api_module.Api()
    client.control("abc", "play")

    assert seen == [("/control/abc", "POST")], seen


def test_journal_keeps_old_lines_intact(tmp_path) -> None:  # noqa: ANN001
    """Строки прошлой сессии выводятся как есть, без нового времени.

    Раньше старая строка грузилась как запись с at=0, и line() приписывала
    ей время текущего запуска: «00:00:00 [info] 00:04:14 [boot] …».
    """
    from tvhost2.core.diagnostics import Journal

    path = tmp_path / "overlay.log"
    path.write_text("00:04:14 [boot] === запуск ===\n", encoding="utf-8")

    journal = Journal(path=path)
    lines = journal.tail_text(10)

    assert lines == "00:04:14 [boot] === запуск ===", lines
    assert "[info]" not in lines


def test_ui_entry_point_exists() -> None:
    """Окно должно запускаться одной командой: `python -m tvhost2.ui`.

    Без `__main__.py` в пакете ui интерфейс вообще не стартовал — сервер
    поднимался, а окна не было ни в одной инструкции.
    """
    import importlib

    module = importlib.import_module("tvhost2.ui.__main__")
    assert callable(module.main)


def test_ui_entry_point_accepts_serve_flag() -> None:
    """Ключ --serve поднимает прокси в том же процессе — одна команда для заказчика."""
    import inspect

    from tvhost2.ui import __main__ as entry

    source = inspect.getsource(entry.main)
    assert "--serve" in source
    assert "--port" in source


def test_serve_mode_stops_server_on_exit() -> None:
    """Прокси в режиме --serve останавливается и поток дожидается.

    Прокси живёт в фоновом потоке. Если не остановить его перед закрытием
    окна, поток обращается к уже разрушенным объектам Qt: в консоли
    «pure virtual method called», процесс падает с трейсбеком. Тест
    проверяет механику остановки: shutdown + join + закрытие сокета.
    """
    import time

    from tvhost2.ui.__main__ import _start_server

    port = 9403
    httpd = _start_server("127.0.0.1", port)
    worker = getattr(httpd, "_worker", None)
    assert worker is not None and worker.is_alive(), "поток сервера не запустился"

    # Сервер реально отвечает, пока работает
    import urllib.request

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:  # noqa: S310
        assert response.status == 200

    # Останавливаем ровно так, как это делает main()
    httpd.shutdown()
    worker.join(timeout=5)
    httpd.server_close()

    assert not worker.is_alive(), "поток сервера остался жить после shutdown"

    # Порт освобождён: повторный запуск на нём не падает
    time.sleep(0.2)
    again = _start_server("127.0.0.1", port)
    again.shutdown()
    getattr(again, "_worker", None).join(timeout=5) if getattr(again, "_worker", None) else None
    again.server_close()
