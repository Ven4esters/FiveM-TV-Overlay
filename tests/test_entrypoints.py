"""Тест точек входа: каждую команду запуска можно выполнить буквально.

ERR-057 и ERR-058 показали, откуда берутся «мёртвые» ярлыки: код переехал
в пакет, а снаружи остались вызовы файлов, которых больше нет. Проверять
такое глазами бесполезно — здесь каждая точка входа извлекается из
исходников и проверяется так, как её выполняет система.

Правило (CONTRIBUTING.md): точки входа запускаются как модули пакета
(``python -m tvhost.<модуль>``), а не файлами в корне проекта.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PKG = SRC / "tvhost"


# ---------- источник запусков: .bat и ProxyController ----------


def _bat_files() -> list[Path]:
    return sorted(p for p in ROOT.glob("*.bat"))


def _bat_commands() -> list[tuple[str, str]]:
    """Все команды вида ``python.exe ...`` внутри .bat — с именем файла."""
    out: list[tuple[str, str]] = []
    for path in _bat_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("REM"):
                continue
            if ".py" not in stripped and "-m " not in stripped:
                continue
            if "python" not in stripped.lower():
                continue
            out.append((path.name, stripped))
    return out


def _module_targets(commands: list[tuple[str, str]]) -> list[str]:
    """Имена модулей из конструкций ``-m tvhost.foo``."""
    targets: list[str] = []
    for _name, cmd in commands:
        for match in re.finditer(r"-m\s+(tvhost[\w.]*)", cmd):
            targets.append(match.group(1))
    return targets


@pytest.mark.parametrize("bat", _bat_files(), ids=lambda p: p.name)
def test_bat_does_not_call_python_files_in_root(bat: Path) -> None:
    """.bat не запускает .py-файлы: в корне их нет, только пакет в src/."""
    text = bat.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("REM") or "python" not in stripped.lower():
            continue
        # ищем «что-то.py» в реальной команде
        for ref in re.findall(r"[\w./\\-]+\.py\b", stripped):
            if ref.startswith("tvhost"):
                continue  # упоминание в тексте сообщения
            raise AssertionError(
                f"{bat.name}: запускает файл {ref!r}, а его в корне нет. "
                "Запускайте модуль: python -m tvhost.<модуль>"
            )


def test_module_targets_from_bat_exist() -> None:
    """Каждый ``-m tvhost.X`` из .bat указывает на существующий модуль."""
    targets = _module_targets(_bat_commands())
    assert targets, "в .bat не нашлось ни одной команды запуска пакета"
    for target in targets:
        assert _module_path(target), f"модуль {target} не найден в {PKG}"


def test_proxy_controller_starts_module_not_file() -> None:
    """ProxyController поднимает прокси как модуль, а не файлом proxy.py."""
    source = (PKG / "ui" / "overlay.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    spawn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_spawn":
            spawn = node
            break
    assert spawn is not None, "в overlay.py пропал ProxyController._spawn"

    # первый аргумент Popen — список команды; разбираем его по узлам
    command: list[str] | None = None
    for node in ast.walk(spawn):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "Popen":
            first = node.args[0] if node.args else None
            if isinstance(first, ast.List):
                command = [
                    elt.value if isinstance(elt, ast.Constant) else "<expr>" for elt in first.elts
                ]
            break
    assert command is not None, "в _spawn нет subprocess.Popen со списком аргументов"

    assert "-m" in command, f"нет ключа -m в команде запуска: {command}"
    assert "tvhost.proxy" in command, f"прокси запускается не как модуль: {command}"
    files = [c for c in command if c.endswith(".py")]
    assert not files, f"_spawn всё ещё вызывает файл напрямую: {files}"


def test_proxy_controller_sets_pythonpath(src_root: Path) -> None:
    """Без PYTHONPATH дочерний python не найдёт пакет в src/."""
    source = (PKG / "ui" / "overlay.py").read_text(encoding="utf-8")
    assert 'env["PYTHONPATH"]' in source, (
        "ProxyController не выставляет PYTHONPATH — прокси не запустится "
        "из установки, где src/ не в sys.path"
    )


# ---------- вспомогательное ----------


def _module_path(dotted: str) -> Path | None:
    """tvhost.proxy -> src/tvhost/proxy.py (или .../proxy/__init__.py)."""
    parts = dotted.split(".")
    base = SRC.joinpath(*parts)
    if base.with_suffix(".py").exists():
        return base.with_suffix(".py")
    if (base / "__init__.py").exists():
        return base / "__init__.py"
    return None


@pytest.fixture(scope="module")
def src_root() -> Path:
    return SRC
