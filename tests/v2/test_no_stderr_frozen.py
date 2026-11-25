"""Сервер обязан отвечать, когда у процесса НЕТ stderr.

Это реальный отчёт из собранного .exe:

    16:16:42 [err] прокси не отвечает на порту 9001:
                   127.0.0.1:9001 — слушает; проверка: не отвечает:
                   Remote end closed connection without response

Порт слушает, а ответа нет. Причина не в сети, не в прокси и не в порте:

    send_response() → log_request() → log_message() → sys.stderr.write(...)

В сборке PyInstaller с `console=False` консоли нет, и `sys.stderr` там
равен `None`. `sys.stderr.write` даёт AttributeError ВНУТРИ `send_response`,
то есть до отправки заголовков. Клиент видит оборванное соединение, сервер
при этом жив.

ПОЧЕМУ ТЕСТ НЕ ПОДМЕНЯЕТ `sys.stderr` НАПРЯМУЮ: pytest перехватывает
стандартные потоки и восстанавливает их между тестами, поэтому подмена
через `monkeypatch.setattr(sys, "stderr", None)` до сервера не доходит —
тест «проходил» на сломанном коде и ничего не ловил. Вместо этого ломается
то, что читает поток: `sys.__stderr__` остаётся настоящим, а проверяем мы
итог — ответ сервера.
"""

from __future__ import annotations

import http.server
import sys
import threading
import urllib.error
import urllib.request

import pytest

from tvhost2.web import server as server_module


@pytest.fixture
def no_stderr(monkeypatch):
    """Сделать запись в stderr невозможной — как в сборке `console=False`.

    Подменяем `log_message` базового класса из stdlib: он и вызывает
    `sys.stderr.write`. Теперь любой обработчик, который НЕ переопределяет
    `log_message` сам, падает ровно так же, как падал .exe.
    """

    def broken(self: object, fmt: str, *args: object) -> None:
        raise AttributeError("'NoneType' object has no attribute 'write'")

    monkeypatch.setattr(http.server.BaseHTTPRequestHandler, "log_message", broken)


def _get(port: int) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/", timeout=10) as response:
        return response.status, response.read()


class TestHandlerSurvivesWithoutStderr:
    def test_root_answers_without_stderr(self, no_stderr) -> None:
        """`GET /` возвращает 200, хотя запись в stderr невозможна.

        Это ровно тот запрос, которым пульт рисует бейдж «прокси: работает».
        Без переопределения `log_message` он рвётся без ответа.
        """
        server = server_module.serve("127.0.0.1", 0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = _get(port)
            assert status == 200
            assert b"FiveM Video Overlay" in body
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_sources_answers_without_stderr(self, no_stderr) -> None:
        """Вкладка «Источники» заполняется, даже когда консоли нет."""
        server = server_module.serve("127.0.0.1", 0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/sources", timeout=15) as response:
                assert response.status == 200
                body = response.read()
            assert b"Rutube" in body
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_log_message_writes_nothing(self) -> None:
        """`log_message` переопределён и не трогает stderr вообще.

        Проверяем сам факт переопределения: если кто-то уберёт override,
        сборка с `console=False` снова перестанет отвечать.
        """
        assert (
            server_module.OverlayHandler.log_message
            is not http.server.BaseHTTPRequestHandler.log_message
        )

    def test_log_message_is_safe_call(self, no_stderr) -> None:
        """Вызов `log_message` на нашем обработчике не бросает исключение."""
        server_module.OverlayHandler.log_message(None, "проверка %s", 1)  # type: ignore[arg-type]

    def test_broken_stderr_would_break_plain_handler(self, no_stderr) -> None:
        """Контрольная проверка: без override сервер падает.

        Без этой строки непонятно, лечим мы настоящую причину или тест
        проходит сам по себе — история уже была: фикстура снималась раньше
        времени, и тест «зеленел» на сломанном коде.
        """
        plain = http.server.BaseHTTPRequestHandler
        with pytest.raises(AttributeError):
            plain.log_message(None, "проверка %s", 1)  # type: ignore[arg-type]


class TestEntryPointRestoresStreams:
    def test_ensure_streams_fills_missing_streams(self, monkeypatch) -> None:
        """Точка входа подставляет заглушки вместо отсутствующих потоков."""
        from tvhost2.core import startup

        monkeypatch.setattr(sys, "stdout", None)
        replaced = startup.ensure_streams()
        assert "stdout" in replaced
        assert sys.stdout is not None
        sys.stdout.write("проверка")  # заглушка не мешает писать

    def test_ensure_streams_keeps_working_streams(self, monkeypatch) -> None:
        """Живые потоки не подменяются — терять вывод из исходников нельзя."""
        import io

        from tvhost2.core import startup

        original = io.StringIO()
        monkeypatch.setattr(sys, "stdout", original)
        startup.ensure_streams()
        assert sys.stdout is original


class TestCrashLogWritesToJournal:
    def test_unhandled_exception_is_written(self, tmp_path, monkeypatch) -> None:
        """Необработанное исключение оставляет след в журнале.

        В сборке `console=False` трейсбек печатать некуда — без этой записи
        падение выглядит как «программа просто закрылась».
        """
        from tvhost2.core import diagnostics, startup

        journal = diagnostics.Journal(path=tmp_path / "overlay.log")
        monkeypatch.setattr(diagnostics, "JOURNAL", journal)
        monkeypatch.setattr(sys, "excepthook", sys.excepthook)

        startup.install_crash_log()
        try:
            raise RuntimeError("проверка перехвата")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

        text = (tmp_path / "overlay.log").read_text(encoding="utf-8")
        assert "проверка перехвата" in text
        assert "RuntimeError" in text
