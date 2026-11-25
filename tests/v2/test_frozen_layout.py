"""Собранный .exe ведёт себя иначе, чем запуск из исходников.

Три различия, каждое из которых ломает программу молча:

  1. Файлы данных (`hls.min.js`, `style.qss`, иконки) в onefile-сборке
     лежат не рядом с исходниками, а во временной папке распаковки.
     `Path(__file__).parent` ведёт туда, и это работает — но только пока
     мы не пытаемся рядом ПИСАТЬ.
  2. Писать нужно рядом с .exe (`sys.executable`), а не в распаковку:
     её удаляют при выходе, и настройки исчезают.
  3. Двойной клик не передаёт аргументов. `--serve` по умолчанию выключен
     для исходников (`start.bat` передаёт его явно), но в .exe сервер
     обязан подняться сам — иначе пульт открывается без прокси и пуст.

Тесты дешёвые и не запускают Qt: проверяется только выбор путей и флаг.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tvhost2.core import resources


class TestResourcePaths:
    def test_source_mode_uses_package(self, monkeypatch) -> None:
        monkeypatch.setattr(resources, "FROZEN", False)
        assert resources.resource("web", "static").is_dir()

    def test_frozen_mode_uses_meipass(self, monkeypatch, tmp_path) -> None:
        """В сборке данные лежат в <распаковка>/tvhost2/... — проверяем именно это."""
        monkeypatch.setattr(resources, "FROZEN", True)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        (tmp_path / "tvhost2" / "web" / "static").mkdir(parents=True)
        assert resources.resource("web", "static") == tmp_path / "tvhost2" / "web" / "static"

    def test_frozen_app_dir_is_next_to_exe(self, monkeypatch, tmp_path) -> None:
        """Рядом с .exe — единственное место, которое переживает перезапуск."""
        monkeypatch.setattr(resources, "FROZEN", True)
        monkeypatch.setattr(sys, "executable", str(tmp_path / "FiveM.exe"), raising=False)
        assert resources.app_dir() == tmp_path

    def test_source_app_dir_is_project_root(self, monkeypatch) -> None:
        monkeypatch.setattr(resources, "FROZEN", False)
        root = resources.app_dir()
        assert (root / "pyproject.toml").is_file(), f"корень проекта не найден: {root}"

    def test_every_bundled_file_exists_in_source(self) -> None:
        """Всё, что spec кладёт в .exe, обязано существовать и в исходниках."""
        assert resources.resource("web", "static", "hls.min.js").is_file()
        assert resources.resource("ui", "assets", "style.qss").is_file()
        icons = resources.resource("ui", "assets", "icons")
        assert icons.is_dir() and any(icons.iterdir()), "папка иконок пуста"


class TestServeDefault:
    """Повторяет определение флага из ui/__main__.py, не импортируя Qt."""

    @staticmethod
    def _parse(frozen: bool, argv: list[str]) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--serve",
            action=argparse.BooleanOptionalAction,
            default=frozen,
        )
        return parser.parse_args(argv)

    def test_exe_starts_server_without_arguments(self) -> None:
        """Двойной клик по .exe: аргументов нет, а сервер нужен."""
        assert self._parse(True, []).serve is True

    def test_source_does_not_start_server_implicitly(self) -> None:
        """Из исходников поведение не меняем: сервер поднимает start.bat."""
        assert self._parse(False, []).serve is False

    def test_explicit_flags_win(self) -> None:
        assert self._parse(True, ["--no-serve"]).serve is False
        assert self._parse(False, ["--serve"]).serve is True


class TestBuildSpec:
    """tvhost.spec — конфигурация сборки; ошибка в ней видна через 10 минут."""

    def test_spec_parses(self) -> None:
        import ast

        spec = Path(__file__).resolve().parents[2] / "tvhost.spec"
        assert spec.is_file(), "tvhost.spec пропал — build.bat нечего собирать"
        ast.parse(spec.read_text(encoding="utf-8"))

    def test_spec_bundles_datas(self) -> None:
        spec = Path(__file__).resolve().parents[2] / "tvhost.spec"
        text = spec.read_text(encoding="utf-8")
        for needed in ("hls.min.js", "style.qss", "icons", "app.ico"):
            assert needed in text, f"{needed} не попадает в сборку"

    def test_entry_point_uses_absolute_import(self) -> None:
        """PyInstaller исполняет точку входа как модуль ВНЕ пакета.

        Если в spec указать `ui/__main__.py` напрямую, относительные импорты
        внутри него падают в .exe с ImportError: attempted relative import
        with no known parent package. Проверено на реальной сборке.
        """
        root = Path(__file__).resolve().parents[2]
        spec = (root / "tvhost.spec").read_text(encoding="utf-8")
        assert "tvhost2_entry.py" in spec, "spec должен указывать на обёртку"
        assert 'PKG / "ui" / "__main__.py"' not in spec, "нельзя указывать ui/__main__.py напрямую"

        entry = (root / "src" / "tvhost2_entry.py").read_text(encoding="utf-8")
        assert "from tvhost2.ui.__main__ import main" in entry, "нужен абсолютный импорт"
        assert not any(
            line.startswith("from .") or line.startswith("from ..") for line in entry.splitlines()
        ), "в точке входа не должно быть относительных импортов"

    def test_spec_sets_exe_icon(self) -> None:
        """Без icon= у .exe пустой значок Windows, и заметить это можно
        только после сборки — то есть через десять минут."""
        spec = Path(__file__).resolve().parents[2] / "tvhost.spec"
        text = spec.read_text(encoding="utf-8")
        assert "icon=ICON" in text, "в EXE() не передан icon="
        assert (
            Path(__file__).resolve().parents[2] / "src" / "tvhost2" / "ui" / "assets" / "app.ico"
        ).is_file(), "app.ico пропал из репозитория"

    def test_app_ico_has_all_sizes(self) -> None:
        """Windows берёт из .ico нужный размер: без 16 значок на панели задач
        размывается, без 256 — выглядит пустым в проводнике.

        Файл читается напрямую, без импорта theme: PySide6 в тестах сборки
        не нужен, а падение импорта Qt не должно выглядеть поломкой иконки.
        """
        ico = Path(__file__).resolve().parents[2] / "src" / "tvhost2" / "ui" / "assets" / "app.ico"
        assert ico.is_file(), "app.ico пропал из репозитория"
        raw = ico.read_bytes()
        # Заголовок .ico: reserved(2) type(2) count(2), затем count записей по 16 байт
        assert raw[:2] == b"\x00\x00" and raw[2:4] == b"\x01\x00", "это не .ico"
        count = int.from_bytes(raw[4:6], "little")
        sizes = set()
        for i in range(count):
            entry = raw[6 + i * 16 : 6 + (i + 1) * 16]
            w = entry[0] or 256
            sizes.add(w)
        for needed in (16, 32, 48, 256):
            assert needed in sizes, f"в .ico нет размера {needed}: {sorted(sizes)}"
