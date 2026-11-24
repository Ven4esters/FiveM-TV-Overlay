"""Точка входа FiveM Video Overlay 2.0.

Запускает только сервер: страницу плеера открывает телевизор в игре, а
оверлей (на PySide6) подключается к тому же серверу по HTTP.
"""

from __future__ import annotations

import logging

from .web.server import main

if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    main()
