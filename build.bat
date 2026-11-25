@echo off
REM ============================================================================
REM  FiveM Video Overlay — сборка .exe
REM
REM  Один файл без установки: пользователю достаточно скачать и запустить.
REM  Рядом с .exe ложатся .md5 и .sha256 — по ним проверяют, что файл
REM  скачался целиком и не подменён.
REM
REM  Что нужно: Python 3.10+ (тот же, из которого работает start.bat)
REM  Сколько занимает: 3–10 минут, из них большая часть — первый запуск
REM  PyInstaller, он распаковывает свои файлы.
REM
REM  Результат: dist\FiveM-Video-Overlay.exe, .exe.md5, .exe.sha256
REM ============================================================================
chcp 65001 >nul 2>&1
setlocal EnableExtensions EnableDelayedExpansion

if not defined PYTHONIOENCODING set "PYTHONIOENCODING=utf-8"
if not defined PYTHONUTF8 set "PYTHONUTF8=1"

cd /d "%~dp0"
title FiveM Video Overlay — сборка .exe

set "VENV_PY=.venv\Scripts\python.exe"
set "APP_NAME=FiveM-Video-Overlay"
set "OUT_DIR=dist"
set "EXE=%OUT_DIR%\%APP_NAME%.exe"

echo ============================================================
echo   FiveM Video Overlay — сборка .exe
echo ============================================================
echo.

REM ---------------------------------------------------------------------------
REM  1. Python
REM ---------------------------------------------------------------------------
echo [1/6] Python:
REM Проверяем окружение проекта: если оно уже есть, собираем им же. Иначе
REM собирающий и запускающий Python могут оказаться разными разрядностями,
REM и .exe соберётся не таким, каким его ждут.
if exist "%VENV_PY%" (
    set "PY=%VENV_PY%"
) else (
    where python >nul 2>&1
    if not errorlevel 1 (
        set "PY=python"
    ) else (
        echo   [ОШИБКА] Python не найден.
        echo.
        echo   Сначала запустите start.bat — он создаст окружение .venv.
        echo   Или поставьте Python 3.12 с python.org и отметьте
        echo   "Add python.exe to PATH".
        goto :FAIL
    )
)
"%PY%" -c "import sys; print('  ' + sys.version.split()[0] + ' из ' + sys.executable)"
if errorlevel 1 goto :FAIL

REM 32-битный Python соберёт .exe, который не запустится у большинства:
REM PySide6 не имеет 32-битных сборок.
"%PY%" -c "import sys; sys.exit(0 if sys.maxsize > 2**32 else 1)"
if errorlevel 1 (
    echo   [ОШИБКА] Python 32-битный. Нужен 64-битный: PySide6 не собирается под 32 бита.
    goto :FAIL
)
echo.

REM ---------------------------------------------------------------------------
REM  2. Зависимости
REM ---------------------------------------------------------------------------
echo [2/6] Зависимости:
"%PY%" -c "import PySide6, yt_dlp" >nul 2>&1
if errorlevel 1 (
    echo   Ставлю runtime-зависимости...
    "%PY%" -m pip install -r requirements-v2.txt --quiet
    if errorlevel 1 (
        echo   [ОШИБКА] Не удалось поставить зависимости.
        goto :FAIL
    )
)

REM PyInstaller не входит в runtime: он нужен только для сборки, поэтому
REM ставится здесь, а не в requirements-v2.txt — иначе он поедет внутрь .exe.
"%PY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo   Ставлю PyInstaller (только для сборки, в .exe не попадёт)...
    "%PY%" -m pip install "pyinstaller>=6.0" --quiet
    if errorlevel 1 (
        echo   [ОШИБКА] Не удалось поставить PyInstaller.
        goto :FAIL
    )
)
"%PY%" -c "import PyInstaller; print('  PyInstaller ' + PyInstaller.__version__)"
echo.

REM ---------------------------------------------------------------------------
REM  3. Чистка прошлой сборки
REM ---------------------------------------------------------------------------
echo [3/6] Чистка:
REM Старые build\ и dist\ опасны не местом, а путаницей: PyInstaller
REM переиспользует промежуточные файлы, и в .exe может уехать модуль,
REM которого в коде уже нет. Сборка начинается с нуля.
REM build\ чистит сам PyInstaller по ключу --clean (шаг 4); здесь — только
REM dist\, потому что старый .exe иначе останется лежать рядом с новым, и
REM легко раздать не тот файл.
if exist "%OUT_DIR%" rmdir /s /q "%OUT_DIR%" 2>nul
echo   прошлый %OUT_DIR%\ удалён
echo.

REM ---------------------------------------------------------------------------
REM  4. Сборка
REM ---------------------------------------------------------------------------
echo [4/6] Сборка .exe (3–10 минут, это нормально):
echo.
"%PY%" -m PyInstaller tvhost.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo   [ОШИБКА] PyInstaller завершился с ошибкой. Полный журнал — build\%APP_NAME%\warn-%APP_NAME%.txt
    goto :FAIL
)
echo.
if not exist "%EXE%" (
    echo   [ОШИБКА] Сборка прошла, но %EXE% не появился.
    goto :FAIL
)

for %%F in ("%EXE%") do set "SIZE=%%~zF"
set /a SIZE_MB=!SIZE! / 1048576
echo   готово: %EXE%  ^(!SIZE_MB! МБ^)
echo.

REM ---------------------------------------------------------------------------
REM  5. Контрольные суммы
REM ---------------------------------------------------------------------------
echo [5/6] Контрольные суммы:
REM Формат вывода certutil: «MD5 хеш алгоритма для файла:» на одной строке,
REM сам хеш — на следующей. Ищем строку из одних шестнадцатеричных цифр,
REM а не режем по позиции: текст вокруг переведён и меняется между версиями
REM Windows, а сам хеш — нет.
set "MD5="
set "SHA="
REM Внимание на регистр: certutil печатает хеш ЗАГЛАВНЫМИ, а findstr без /i
REM не найдёт ни одной строки — MD5 останется пустым, и сборка «упадёт» на
REM ровном месте при существующем файле. Это не теория: проверено.
REM Хеш приводим к нижнему регистру сами, чтобы совпадал с тем, что печатают
REM sha256sum и Get-FileHash, и с тем, что вписано в README.
for /f "delims=" %%L in ('certutil -hashfile "%EXE%" MD5 ^| findstr /r /i "^[0-9a-f][0-9a-f]*$"') do if not defined MD5 set "MD5=%%L"
for /f "delims=" %%L in ('certutil -hashfile "%EXE%" SHA256 ^| findstr /r /i "^[0-9a-f][0-9a-f]*$"') do if not defined SHA set "SHA=%%L"

REM Приводим к нижнему регистру — ровно то, что печатают sha256sum и
REM Get-FileHash. Регистр на проверку не влияет (`sha256sum -c` принимает
REM и заглавные), но читатель сравнивает глазами, а «D168…» и «d168…» —
REM две разные строки на вид, и это лишний повод для вопроса.
if defined MD5  set "MD5=!MD5:A=a!" & set "MD5=!MD5:B=b!" & set "MD5=!MD5:C=c!" & set "MD5=!MD5:D=d!" & set "MD5=!MD5:E=e!" & set "MD5=!MD5:F=f!"
if defined SHA  set "SHA=!SHA:A=a!" & set "SHA=!SHA:B=b!" & set "SHA=!SHA:C=c!" & set "SHA=!SHA:D=d!" & set "SHA=!SHA:E=e!" & set "SHA=!SHA:F=f!"

if not defined MD5 (
    echo   [ОШИБКА] Не удалось посчитать MD5 — certutil недоступен?
    goto :FAIL
)
if not defined SHA (
    echo   [ОШИБКА] Не удалось посчитать SHA256.
    goto :FAIL
)

REM Файлы в формате, который понимают обе стороны: `md5sum -c` и
REM `sha256sum -c` на Linux, PowerShell `Get-FileHash` — на Windows.
REM Две пробела между хешем и именем — это формат «текстовый», не «двоичный»:
REM с одним пробелом часть инструментов считает файл бинарным.
> "%EXE%.md5"    echo !MD5!  %APP_NAME%.exe
> "%EXE%.sha256" echo !SHA!  %APP_NAME%.exe

echo   MD5:    !MD5!
echo   SHA256: !SHA!
echo   записаны в %EXE%.md5 и %EXE%.sha256
echo.

REM ---------------------------------------------------------------------------
REM  6. Итог
REM ---------------------------------------------------------------------------
echo [6/6] Готово.
echo.
echo ============================================================
echo   Файлы для раздачи:
echo     %EXE%
echo     %EXE%.md5
echo     %EXE%.sha256
echo.
echo   Проверить перед раздачей:
echo     PowerShell:  Get-FileHash "%EXE%" -Algorithm SHA256
echo     Linux/macOS: sha256sum -c %APP_NAME%.exe.sha256
echo.
echo   Приложение оконное: журнал смотрите на вкладке «Журнал» пульта,
echo   а не в консоли — её у .exe нет.
echo ============================================================
echo.
pause
exit /b 0

:FAIL
echo.
echo ============================================================
echo   СБОРКА НЕ УДАЛАСЬ — .exe не создан.
echo   Текст ошибки выше. Если не помогло — пришлите его целиком.
echo ============================================================
echo.
pause
exit /b 1
