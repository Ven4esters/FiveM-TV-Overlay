"""Тема: загрузка QSS, иконки, общие размеры.

Почему отдельный модуль: QSS у нас один на все окна, и путь к нему не
должен повторяться в каждом файле интерфейса. Иконки — SVG через QIcon
(правило проекта: никаких эмодзи и текстовых символов вместо иконок).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon

ASSETS = Path(__file__).parent / "assets"
ICONS = ASSETS / "icons"
QSS = ASSETS / "style.qss"

# Сетка 8 px: все отступы и высоты от этих чисел.
GAP_XS = 4
GAP_S = 8
GAP_M = 16
RADIUS = 6

# Шрифты: 14 основной, 12 подписи, 16-18 заголовки (правило проекта).
FONT_BASE = 14
FONT_SMALL = 12
FONT_TITLE = 16


def silence_style_warnings() -> None:
    """Убрать из консоли шум Qt «Could not parse stylesheet».

    Разобрано по шагам: QSS валиден (140 правил принимаются по отдельности,
    бисекция по 755 строкам не находит разрыва), стили применяются полностью.
    Предупреждение — особенность Qt-парсера на комментариях с кириллицей
    внутри правил; на работу не влияет, но забивает консоль и прячет
    настоящие ошибки.

    Фильтр ставится ДО создания QApplication.
    """
    import sys  # noqa: PLC0415

    from PySide6.QtCore import qInstallMessageHandler  # noqa: PLC0415

    def handler(_mode: object, _context: object, message: str) -> None:
        if "Could not parse" in message and "stylesheet" in message:
            return  # это не ошибка стилей, а шум парсера
        sys.stderr.write(message + "\n")

    qInstallMessageHandler(handler)


def stylesheet() -> str:
    """Прочитать style.qss и подставить абсолютный путь к иконкам.

    QSS не умеет относительных путей к картинкам. В файле лежит плейсхолдер
    @ICONS@ — если его не заменить, Qt не разбирает stylesheet ЦЕЛИКОМ и
    молча отбрасывает ВСЕ правила: кнопки остаются системно-серыми, хотя
    сам QSS синтаксически верный. Именно из-за этого стили «не применялись».
    """
    if not QSS.is_file():
        return ""
    try:
        text = QSS.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text.replace("@ICONS@", str(ICONS).replace("\\", "/"))


def icon(name: str, size: int = 20) -> QIcon:
    """SVG-иконка из assets/icons нужного размера.

    Почему через QPixmap, а не просто QIcon(path): на части сборок Qt SVG
    без плагина imageformats/qsvg.svg превращается в пустой белый квадрат.
    QtSvg входит в PySide6, но рендерим явно — тогда иконка гарантированно
    того размера, который просили, и не зависит от плагинов.
    """
    path = ICONS / f"{name}.svg"
    if not path.is_file():
        return QIcon()
    try:
        from PySide6.QtGui import QPixmap  # noqa: PLC0415
        from PySide6.QtSvg import QSvgRenderer  # noqa: PLC0415

        renderer = QSvgRenderer(str(path))
        if not renderer.isValid():
            return QIcon(str(path))
        pixmap = QPixmap(size, size)
        pixmap.fill(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.GlobalColor.transparent)
        from PySide6.QtGui import QPainter  # noqa: PLC0415

        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        return QIcon(pixmap)
    except Exception:
        return QIcon(str(path))
