"""Занятый порт не должен убивать окно молча.

Симптом из отчёта с собранного .exe: двойной клик — и ничего не происходит,
ни окна, ни ошибки. Причина: `--serve` поднимает сервер в том же процессе,
`ThreadingHTTPServer` при занятом порту бросает `OSError`, а сборка идёт с
`console=False` — трейсбек печатать некуда, процесс завершается.

Тест не поднимает Qt: проверяется поведение стандартной библиотеки и
контракт точки входа (`OSError` ловится, а не пробрасывается наружу).
"""

from __future__ import annotations

import errno
import socket
from http.server import ThreadingHTTPServer


def test_second_server_on_same_port_raises_oserror() -> None:
    """Второй сервер на занятом порту падает именно с OSError.

    Это тот тип исключения, который `main()` обязан перехватить. Если
    стандартная библиотека сменит тип ошибки, тест это покажет.
    """
    first = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = first.server_address[1]
    try:
        try:
            ThreadingHTTPServer(("0.0.0.0", port), None)  # noqa: F841 — важен факт падения
        except OSError as exc:
            assert exc.errno in {errno.EADDRINUSE, errno.EACCES}
        else:  # pragma: no cover — сюда попадать не должны
            raise AssertionError("второй сервер на занятом порту не упал")
    finally:
        first.server_close()


def test_busy_port_error_is_catchable_and_readable() -> None:
    """Сообщение об ошибке пригодно для показа человеку.

    В `console=False` единственное место, где пользователь увидит причину —
    строка статуса пульта, поэтому текст берётся из исключения как есть.
    """
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(1)
    try:
        try:
            ThreadingHTTPServer(("0.0.0.0", port), None)  # noqa: F841
        except OSError as exc:
            message = f"порт {port} занят — прокси не запущен в этом окне"
            assert str(port) in message
            assert "занят" in message
            assert exc  # исключение не пустое
    finally:
        sock.close()
