"""
Точка входа пакета.

Запуск оверлея:   python -m tvhost
Запуск прокси:    python -m tvhost.proxy --port 9000
"""

from __future__ import annotations


def main() -> None:
    """Запустить панель управления (оверлей)."""
    # Импорт внутри функции намеренный: Qt загружается только при запуске
    # панели, чтобы `python -m tvhost.proxy` не тянул GUI-зависимости.
    from .ui import overlay  # noqa: PLC0415

    overlay.main()


if __name__ == "__main__":
    main()
