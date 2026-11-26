# -*- mode: python ; coding: utf-8 -*-
"""Сборка FiveM Video Overlay в один .exe. Запускается из build.bat.

Почему spec, а не десяток флагов в командной строке: путей данных у проекта
четыре (страница плеера, hls.min.js, стиль и иконки), и в командной строке
их легко потерять при правке. Здесь они рядом и с пояснением.

Что важно понимать про --onefile: приложение распаковывается во временную
папку, и всё, что программа должна ПОМНИТЬ между запусками (настройки,
videos/), обязано писаться рядом с .exe, а не в распакованную копию.
Разделение путей живёт в `src/tvhost2/core/resources.py`.
"""

import sys
from pathlib import Path

PROJECT = Path(SPECPATH).resolve()          # корень проекта
SRC = PROJECT / "src"
PKG = SRC / "tvhost2"


def _ffmpeg_binaries() -> list[tuple[str, str]]:
    """Положить ffmpeg из imageio-ffmpeg ВНУТРЬ сборки.

    Почему руками, хотя PyInstaller вроде бы умеет сам: `imageio-ffmpeg`
    достаёт бинарник через `importlib.resources.files("imageio_ffmpeg.binaries")`,
    то есть требует НАСТОЯЩУЮ папку с файлом. В `--onefile` пакет
    распаковывается во временную папку, и файл должен быть в списке данных,
    а не «где-то в site-packages». На Linux хук PyInstaller это подхватывал,
    на Windows — нет, отсюда «ffmpeg не найден» у заказчика при том, что в
    исходниках всё находилось.

    Необязательный шаг: если пакета нет, сборка просто пойдёт без ffmpeg
    (замер громкости будет честно говорить «нужен ffmpeg»).
    """
    try:
        import imageio_ffmpeg  # noqa: PLC0415
    except Exception:
        return []
    exe = ""
    try:
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        # Пакет есть, но бинарник не скачался (бывает при установке без
        # сети) — ищем файл в его папке напрямую.
        folder = Path(imageio_ffmpeg.__file__).parent / "binaries"
        found = sorted(folder.glob("ffmpeg*")) if folder.is_dir() else []
        exe = str(found[0]) if found else ""
    if not exe or not Path(exe).is_file():
        return []
    return [(exe, "imageio_ffmpeg/binaries")]

# Данные повторяют структуру пакета: tvhost2/web/static/... тогда
# `resources.resource("..", "web", "static")` находит их и в .exe.
datas = [
    (str(PKG / "web" / "static" / "hls.min.js"), "tvhost2/web/static"),
    (str(PKG / "ui" / "assets" / "style.qss"), "tvhost2/ui/assets"),
    (str(PKG / "ui" / "assets" / "icons"), "tvhost2/ui/assets/icons"),
    (str(PKG / "ui" / "assets" / "app.ico"), "tvhost2/ui/assets"),
]

# Иконка самого .exe: её видят в проводнике, на панели задач и в диспетчере
# задач. Без неё Windows рисует стандартный «пустой» значок, даже если окно
# приложения своё имеет.
ICON = str(PKG / "ui" / "assets" / "app.ico")

# Пакеты, которые PyInstaller не видит из импортов: они подгружаются
# по имени (плагины Qt, движки yt-dlp) и без этого не попадают в сборку.
hiddenimports = [
    "tvhost2",
    "tvhost2.web.server",
    "tvhost2.ui",
    "tvhost2.ui.__main__",
]

# Windows-расширения, которые yt-dlp импортирует динамически: без них
# YouTube и часть сайтов падают с ImportError уже в собранном .exe.
hiddenimports += [
    "yt_dlp",
    "yt_dlp.extractor",
    "yt_dlp.extractor.lazy_extractors",
    "imageio_ffmpeg",
]

# Qt: оставляем только то, что реально используется. Без этого сборка
# тянет все модули Qt (WebEngine, 3D, Multimedia) и весит сотни мегабайт.
excluded = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.Qt3DCore",
    "PySide6.QtMultimedia",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "tkinter",
    "matplotlib",
    "numpy",
    "PIL",
]

# Точка входа — скрипт-обёртка в корне src/, а НЕ ui/__main__.py напрямую.
# PyInstaller исполняет указанный файл как модуль верхнего уровня, вне пакета,
# и относительные импорты внутри ui/__main__.py (`from . import theme`,
# `from ..core import resources`) падают в собранном .exe с ImportError:
# attempted relative import with no known parent package. В исходниках ошибки
# не видно, потому что там запуск идёт как `python -m tvhost2.ui`.
a = Analysis(
    [str(SRC / "tvhost2_entry.py")],
    pathex=[str(SRC)],
    # ffmpeg — данные, а не модуль: без него в собранном .exe не работает
    # ни замер громкости, ни аварийная склейка дорожек.
    binaries=_ffmpeg_binaries(),
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="FiveM-Video-Overlay",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX ломает Qt: экономия 20 МБ не стоит падений
    icon=ICON,          # значок .exe в проводнике и на панели задач
    console=False,      # оконное приложение; журнал — во вкладке «Журнал»
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
