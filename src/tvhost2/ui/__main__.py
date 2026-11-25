"""Окно пульта FiveM Video Overlay 2.0.

Запуск::

    python -m tvhost2.ui            # подключиться к уже запущенному прокси
    python -m tvhost2.ui --serve    # поднять прокси в этом же процессе
    python -m tvhost2.ui --port 9001 --host 0.0.0.0

Почему отдельный модуль, а не код в `__main__`: сервер (`python -m tvhost2`)
и окно нужны по отдельности. Окно можно открыть на другой машине в той же
сети — оно ходит к прокси по HTTP. А можно всё в одном процессе ключом
`--serve`, так проще заказчику: одна команда, одно окно.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from dataclasses import dataclass
from http.server import ThreadingHTTPServer

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ..core import resources
from . import theme
from .api import Api
from .panel import Panel

log = logging.getLogger(__name__)


@dataclass
class _RunningServer:
    """Сервер и его поток — вместе, чтобы их нельзя было перепутать."""

    server: ThreadingHTTPServer
    worker: threading.Thread


def _start_server(host: str, port: int) -> _RunningServer:
    """Поднять прокси в фоновом потоке того же процесса.

    Возвращает сервер: закрывать его обязано вызывающее. Если этого не
    сделать, при закрытии окна поток продолжает жить, а Qt уже разрушил
    объекты — процесс падает с `pure virtual method called`. Раньше падало
    ровно так: окно закрывалось, а в консоли оставался трейсбек.
    """
    from ..web.server import serve  # noqa: PLC0415

    server = serve(host, port)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    # Поток кладём рядом с сервером, а не внутрь него: у ThreadingHTTPServer
    # такого поля нет, и присваивание «в объект» обманывало и читателя,
    # и проверку типов. Возвращаем пару — вызывающий код сам решает,
    # когда дождаться потока.
    return _RunningServer(server=server, worker=worker)


def main(argv: list[str] | None = None) -> int:
    """Точка входа окна. Возвращает код выхода для оболочки."""
    parser = argparse.ArgumentParser(description="FiveM Video Overlay 2.0 — пульт")
    parser.add_argument("--port", type=int, default=9001, help="порт локального прокси")
    parser.add_argument("--host", default="127.0.0.1", help="адрес прокси, к которому подключаться")
    parser.add_argument(
        "--serve",
        action=argparse.BooleanOptionalAction,
        default=resources.FROZEN,
        help=(
            "поднять сервер в этом же процессе (одна команда вместо двух). "
            "В собранном .exe включено по умолчанию: двойной клик не передаёт "
            "аргументов, а пульт без сервера бесполезен"
        ),
    )
    parser.add_argument("--listen", default="0.0.0.0", help="адрес прослушивания для --serve")
    args = parser.parse_args(argv)

    # Фильтр ставится ДО QApplication: иначе первые же сообщения Qt
    # про stylesheet попадут в консоль и напугают заказчика.
    theme.silence_style_warnings()
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings, True)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("FiveM Video Overlay")
    app.setStyleSheet(theme.stylesheet())

    running: _RunningServer | None = None
    if args.serve:
        try:
            running = _start_server(args.listen, args.port)
        except OSError as exc:
            # Порт занят: чаще всего запущен второй экземпляр .exe, либо
            # порт держит другая программа. В `console=False` трейсбек некуда
            # печатать — процесс просто исчезал бы, и человек решил бы, что
            # «программа не запускается». Поэтому окно всё равно открываем:
            # если на порту уже живёт НАШ прокси, пульт к нему подключится
            # и покажет «прокси: работает».
            log.error("прокси не поднялся на порту %s: %s", args.port, exc)

    panel = Panel(api=Api(base=f"http://127.0.0.1:{args.port}"))
    panel.show()
    if args.serve and running is None:
        panel.set_remote_status(f"порт {args.port} занят — прокси не запущен в этом окне")
    try:
        return app.exec()
    finally:
        # Останавливаем прокси ДО разрушения интерфейса. Иначе поток сервера
        # продолжает работать с уже удалёнными объектами Qt — это и давало
        # «pure virtual method called» при закрытии окна.
        if running is not None:
            # shutdown() просит цикл остановиться, но НЕ ждёт его. Без join
            # поток продолжает жить, пока Python разрушает интерпретатор —
            # отсюда «pure virtual method called» и трейсбек при закрытии.
            running.server.shutdown()
            running.worker.join(timeout=5)
            running.server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
