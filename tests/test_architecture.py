"""Тест архитектуры: направление зависимостей между слоями.

Правило из CONTRIBUTING.md («core не импортирует ui/web/proxy») ничего
не стоит, если его нельзя проверить. Этот тест читает исходники через AST
и падает, если зависимости пошли не туда.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "tvhost"

# какой слой что НЕ имеет права импортировать
FORBIDDEN = {
    "core": {"ui", "web", "proxy", "server"},
    "infra": {"ui", "web", "proxy", "server"},
    "web": {"ui", "proxy", "server"},
    "ui": {"proxy", "server"},
}


def _imported_layers(path: Path) -> set[str]:
    """Какие слои пакета импортирует модуль (по относительным импортам)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    layers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            # from .core import x  ->  node.module = "core"
            if node.module:
                layers.add(node.module.split(".")[0])
            else:
                layers.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # from tvhost.core import x
            parts = node.module.split(".")
            if len(parts) > 1 and parts[0] == "tvhost":
                layers.add(parts[1])
    return layers


def _layer_of(path: Path) -> str | None:
    """Слой модуля по его расположению в пакете."""
    relative = path.relative_to(SRC)
    if len(relative.parts) == 1:
        return None if relative.stem == "__init__" else relative.stem
    return relative.parts[0]


@pytest.mark.parametrize(
    "layer",
    sorted(FORBIDDEN),
)
def test_layer_does_not_import_forbidden(layer: str) -> None:
    """Слой не импортирует то, что ему запрещено."""
    violations: list[str] = []
    for path in (SRC / layer).rglob("*.py"):
        imported = _imported_layers(path)
        bad = imported & FORBIDDEN[layer]
        if bad:
            violations.append(f"{path.relative_to(SRC)} → {sorted(bad)}")
    assert not violations, "нарушено направление зависимостей:\n" + "\n".join(violations)


def test_all_modules_compile() -> None:
    """Все модули пакета синтаксически корректны."""
    for path in SRC.rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_no_emoji_in_ui_widgets() -> None:
    """
    В ВИДЖЕТАХ интерфейса эмодзи запрещены: системные шрифты показывают
    их квадратами.

    Консольные сообщения (print в точках входа) — не UI: там эмодзи
    допустимы, `console_io` гарантирует, что они не уронят программу.
    Поэтому проверяются только строки, передаваемые в виджеты Qt.
    """
    emoji_ranges = (
        (0x1F300, 0x1FAFF),  # пиктограммы
        (0x2600, 0x27BF),  # символы и dingbats
    )
    # аргументы этих вызовов попадают на экран внутри виджета
    widget_calls = (
        "setText",
        "setToolTip",
        "setWindowTitle",
        "setPlaceholderText",
        "QLabel",
        "QPushButton",
        "addItem",
        "QMessageBox",
    )
    offenders: list[str] = []

    for path in (SRC / "ui").rglob("*.py"):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not any(call in line for call in widget_calls):
                continue
            for char in line:
                code = ord(char)
                if any(low <= code <= high for low, high in emoji_ranges):
                    offenders.append(f"{path.name}:{line_number}: {char!r} ({hex(code)})")
                    break

    assert not offenders, "эмодзи в виджетах:\n" + "\n".join(offenders[:10])


def test_no_emoji_in_qss() -> None:
    """В файле стилей эмодзи недопустимы (шрифт их не отрисует)."""
    qss = SRC.parents[1] / "style.qss"
    assert qss.is_file(), "style.qss должен лежать рядом с src/"
    text = qss.read_text(encoding="utf-8", errors="replace")
    offenders = [
        f"style.qss:{line}: {char!r}"
        for line, raw in enumerate(text.splitlines(), 1)
        for char in raw
        if 0x1F300 <= ord(char) <= 0x1FAFF or 0x2600 <= ord(char) <= 0x27BF
    ]
    assert not offenders, "эмодзи в QSS:\n" + "\n".join(offenders[:10])


def test_no_setgeometry_for_widgets() -> None:
    """
    Позиционирование виджетов — только через layouts.

    `self.move(...)` у ОКНА — законное исключение: это размещение окна
    на экране и перетаскивание мышью, а не координаты виджета внутри.
    Запрещено `setGeometry` у виджетов и `widget.move(...)` для элементов.
    """
    offenders: list[str] = []
    for path in (SRC / "ui").rglob("*.py"):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if ".setGeometry(" in stripped:
                offenders.append(f"{path.name}:{line_number}: {stripped[:60]}")
                continue
            # self.move — окно; любой другой объект — виджет, это нарушение
            if ".move(" in stripped and not stripped.startswith("self.move("):
                offenders.append(f"{path.name}:{line_number}: {stripped[:60]}")
    assert not offenders, "ручное позиционирование виджетов:\n" + "\n".join(offenders)


def test_all_modules_import_cleanly() -> None:
    """
    Каждый модуль должен импортироваться.

    Это ловит битые импорты, которые не видны статически: `from ffmpeg_bin
    import …` внутри пакета падает только при вызове функции в рантайме.
    Нашлось три таких места при перестройке — тест не даст повторить.
    """
    import importlib

    failures: list[str] = []
    skipped: list[str] = []
    for name in _all_package_modules():
        try:
            importlib.import_module(name)
        except ImportError as exc:
            # Модуль тянет PySide6, а на сервере без графики нет системных
            # библиотек (libEGL.so.1 и подобных). Это не ошибка кода —
            # проверяем отдельно, что модуль хотя бы компилируется
            # (test_all_modules_compile). Здесь такие модули пропускаем.
            if _missing_gui_library(exc):
                skipped.append(f"{name}: {exc}")
                continue
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failures, "модули не импортируются:\n" + "\n".join(failures)


# Системные библиотеки, которых нет на headless-сервере без X11/OpenGL.
_GUI_LIBS = ("libegl", "libgl", "libx11", "libxcb", "libxkbcommon", "libwayland")


def _missing_gui_library(exc: ImportError) -> bool:
    """ImportError вызван отсутствием системной GUI-библиотеки, а не кода."""
    text = str(exc).lower()
    return any(lib in text for lib in _GUI_LIBS) or "cannot open shared object" in text


def _all_package_modules() -> list[str]:
    """
    Список модулей собирается из файлов, а не пишется руками.

    Раньше список был захардкожен и от него отставали: devtools, icons,
    overlay и __main__ никогда не проверялись. Новый модуль в пакете
    теперь попадает под проверку сам.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "tvhost"
    names = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root.parent).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            names.append(".".join(parts))
    return names


def test_module_list_is_not_empty() -> None:
    """Защита от опечатки в пути: пустой список прошёл бы молча."""
    assert len(_all_package_modules()) >= 15


def test_ui_modules_are_covered() -> None:
    """Регресс на ERR-052: UI-слой обязан быть в списке проверяемых модулей."""
    names = _all_package_modules()
    for must in ("tvhost.ui.overlay", "tvhost.ui.icons", "tvhost.__main__"):
        assert must in names, f"{must} выпал из проверки импортов"


def test_gui_skip_list_is_narrow() -> None:
    """
    Пропускать импорт можно только из-за системных GUI-библиотек.

    Иначе правило «нет libEGL — пропускаем» со временем начнёт скрывать
    настоящие ошибки импорта в UI-модулях.
    """
    assert _missing_gui_library(ImportError("libEGL.so.1: cannot open shared object file"))
    assert not _missing_gui_library(ImportError("cannot import name 'Foo' from 'tvhost.core'"))
    assert not _missing_gui_library(ImportError("No module named 'tvhost.nope'"))
