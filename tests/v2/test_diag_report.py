"""Диагностика обязана работать, когда сервер мёртв.

Это главный урок инцидента «прокси: выключен, источники молчат, журнал пуст»:
журнал был ОДНИМ каналом и вёл себя как сервер — если сервер не поднялся,
то и журнала нет. Человек оставался без единой зацепки.

Проверяем три вещи:
  1. журнал пишется в файл рядом с приложением, а не в текущий каталог;
  2. отчёт собирается и без живого сервера, и честно говорит «никто не слушает»;
  3. прокси в отчёте маскируется — иначе пароль уедет в буфер обмена.
"""

from __future__ import annotations

import socket
import urllib.request
from pathlib import Path

import pytest

from tvhost2.core import diag_report, diagnostics


class TestJournalLocation:
    def test_journal_is_next_to_app_not_cwd(self, monkeypatch, tmp_path) -> None:
        """Путь журнала не зависит от того, откуда запустили программу."""
        from tvhost2.core import resources

        monkeypatch.setattr(resources, "app_dir", lambda: tmp_path)
        path = diagnostics._default_log_path()  # noqa: SLF001 — проверяем правило
        assert path.is_relative_to(tmp_path)
        assert path.name == "overlay.log"

    def test_journal_write_creates_file(self, tmp_path) -> None:
        """Запись в журнал реально доходит до диска."""
        journal = diagnostics.Journal(path=tmp_path / "overlay.log")
        journal.log("err", "проверка записи")
        assert (tmp_path / "overlay.log").is_file()
        assert "проверка записи" in (tmp_path / "overlay.log").read_text(encoding="utf-8")

    def test_journal_survives_readonly_dir(self, tmp_path) -> None:
        """Незаписываемый путь не должен ломать приложение.

        Журнал — вспомогательная вещь: если писать некуда, программа обязана
        продолжать работать, а не падать на старте.
        """
        journal = diagnostics.Journal(path=Path("/proc/запрещено/overlay.log"))
        journal.log("err", "эта строка никуда не денется")
        assert journal.tail(1)[0].text == "эта строка никуда не денется"


class TestPortState:
    def test_reports_closed_port(self) -> None:
        """Закрытый порт опознаётся как «никто не слушает»."""
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        # порт освобождён — слушать его больше некому
        assert "никто не слушает" in diag_report.port_state(port, timeout=0.5)

    def test_reports_live_port(self) -> None:
        """Открытый порт опознаётся как «слушает»."""
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.listen(1)
            assert "слушает" in diag_report.port_state(port, timeout=0.5)


class TestProbeWithoutServer:
    def test_probe_reports_no_answer(self) -> None:
        """Проверка без сервера возвращает причину, а не исключение."""
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        result = diag_report.probe(port, timeout=0.5)
        assert "не отвечает" in result

    def test_probe_recognises_our_server(self) -> None:
        """Свой сервер опознаётся по имени приложения в ответе."""
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — имя задано stdlib
                body = json.dumps({"app": diag_report.APP_NAME, "version": "9.9.9"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = diag_report.probe(port, timeout=3)
            assert result.startswith("ok"), result
            assert "9.9.9" in result
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_probe_rejects_foreign_server(self) -> None:
        """Чужой сервер на порту не выдаётся за наш — иначе диагноз будет ложным."""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = b"<html>not us</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert "чужой ответ" in diag_report.probe(port, timeout=3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class TestProxyMasking:
    def test_password_is_hidden(self, monkeypatch) -> None:
        """Логин и пароль прокси в отчёт не попадают.

        Отчёт уходит в буфер обмена, оттуда — в чат. Пароль прокси не имеет
        права туда попасть, даже если прокси корпоративный.
        """
        monkeypatch.setattr(
            urllib.request,
            "getproxies",
            lambda: {"http": "http://user:secret@proxy.local:8080"},
        )
        text = diag_report.proxy_environment()
        assert "secret" not in text
        assert "user" not in text
        assert "proxy.local:8080" in text

    def test_no_proxy(self, monkeypatch) -> None:
        monkeypatch.setattr(urllib.request, "getproxies", dict)
        assert diag_report.proxy_environment() == "нет"


class TestReport:
    def test_report_works_without_server(self) -> None:
        """Отчёт собирается целиком даже когда сервера нет.

        Это и есть сценарий человека: пульт не поднялся, журнал пуст, ему
        нужна хоть какая-то зацепка для переписки.
        """
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        text = diag_report.report(port)
        for expected in ("отчёт", "version:", "порт:", "проверка:", "системный прокси:", "журнал"):
            assert expected in text, f"в отчёте нет «{expected}»: {text[:300]}"

    def test_report_mentions_journal_path(self) -> None:
        """Путь к журналу в отчёте есть — иначе его негде искать руками."""
        text = diag_report.report(59999)
        assert "overlay.log" in text


@pytest.mark.parametrize("kind", ["info", "new", "req", "boot", "err", "player"])
def test_journal_accepts_known_kinds(kind: str) -> None:
    """Все объявленные виды записей принимаются без подмены на info."""
    entry = diagnostics.Journal().log(kind, "проверка")
    assert entry.kind == kind
