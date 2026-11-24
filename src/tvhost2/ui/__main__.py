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
import sys
import threading

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from . import theme
from .api import Api
from .panel import Panel


def _start_server(host: str, port: int) -> None:
    """Поднять прокси в фоновом потоке того же процесса."""
    from ..web.server import serve  # noqa: PLC0415

    server = serve(host, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    """Точка входа окна. Возвращает код выхода для оболочки."""
    parser = argparse.ArgumentParser(description="FiveM Video Overlay 2.0 — пульт")
    parser.add_argument("--port", type=int, default=9001, help="порт локального прокси")
    parser.add_argument("--host", default="127.0.0.1", help="адрес прокси, к которому подключаться")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="поднять прокси в этом же процессе (одна команда вместо двух)",
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

    if args.serve:
        _start_server(args.listen, args.port)

    panel = Panel(api=Api(base=f"http://127.0.0.1:{args.port}"))
    panel.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
