@echo off
REM ============================================================================
REM  FiveM Video Overlay — сборка .exe
REM
REM  Один файл без установки: пользователю достаточно скачать и запустить.
REM  Рядом с .exe ложатся .md5 и .sha256 — по ним проверяют, что файл
REM  скачался целиком и не подменён.
REM
REM  Окружение сборки отдельное: build-venv\. Оно НЕ зависит от .venv,
REM  которым пользуется start.bat, и создаётся само при первом запуске.
REM  Поэтому собрать .exe можно на чистой машине, даже если приложение
REM  ни разу не запускали.
REM
REM  Что нужно: Python 3.10+ (64-бит) из python.org с «Add to PATH».
REM  Сколько занимает: 3–10 минут; первый запуск дольше — ставится окружение.
REM
REM  Результат: dist\FiveM-Video-Overlay.exe, .exe.md5, .exe.sha256
REM ============================================================================
chcp 65001 >nul 2>&1
setlocal EnableExtensions EnableDelayedExpansion

if not defined PYTHONIOENCODING set "PYTHONIOENCODING=utf-8"
if not defined PYTHONUTF8 set "PYTHONUTF8=1"

cd /d "%~dp0"
title FiveM Video Overlay — сборка .exe

set "BUILD_VENV=build-venv"
set "VENV_PY=%BUILD_VENV%\Scripts\python.exe"
set "APP_NAME=FiveM-Video-Overlay"
set "OUT_DIR=dist"
set "EXE=%OUT_DIR%\%APP_NAME%.exe"
set "ICON=src\tvhost2\ui\assets\app.ico"

echo ============================================================
echo   FiveM Video Overlay — сборка .exe
echo ============================================================
echo.

REM ---------------------------------------------------------------------------
REM  1. Python для создания окружения сборки
REM ---------------------------------------------------------------------------
echo [1/7] Python:
REM Ищем базовый Python: он нужен только чтобы создать build-venv.
REM Порядок: py -3 (лаунчер python.org) -> python из PATH.
REM Чужой .venv здесь намеренно НЕ используется: сборка не должна зависеть
REM от того, запускали ли приложение раньше и цел ли .venv.
set "PYEXE="
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PYEXE=py -3"
)
if not defined PYEXE (
    where python >nul 2>&1
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
    echo   [ОШИБКА] Python не найден.
    echo.
    echo   Поставьте Python 3.12 с python.org и отметьте
    echo   "Add python.exe to PATH", затем запустите build.bat снова.
    echo   start.bat для сборки не нужен — окружение создаётся здесь.
    goto :FAIL
)

REM 32-битный Python соберёт .exe, который не запустится у большинства:
REM PySide6 не имеет 32-битных сборок. Проверяем ДО создания окружения,
REM чтобы не тратить десять минут впустую.
%PYEXE% -c "import sys; sys.exit(0 if sys.maxsize > 2**32 else 1)"
if errorlevel 1 (
    echo   [ОШИБКА] Python 32-битный. Нужен 64-битный: PySide6 не собирается под 32 бита.
    goto :FAIL
)
for /f "delims=" %%V in ('%PYEXE% -c "import sys; print(sys.version.split()[0])"') do set "BASE_VER=%%V"
echo   базовый Python !BASE_VER!
echo.

REM ---------------------------------------------------------------------------
REM  2. Окружение сборки build-venv
REM ---------------------------------------------------------------------------
echo [2/7] Окружение сборки %BUILD_VENV%\:
if exist "%VENV_PY%" (
    echo   уже есть — переиспользую
) else (
    echo   создаю (первый раз, это занимает до минуты)...
    %PYEXE% -m venv "%BUILD_VENV%"
    if errorlevel 1 (
        echo   [ОШИБКА] Не удалось создать %BUILD_VENV%.
        echo   Проверьте права на папку и свободное место.
        goto :FAIL
    )
    if not exist "%VENV_PY%" (
        echo   [ОШИБКА] venv создан, но %VENV_PY% не появился.
        echo   Обычно это значит, что Python поставлен из Microsoft Store —
        echo   он создаёт окружения неполно. Поставьте Python с python.org.
        goto :FAIL
    )
    echo   готово
)
"%VENV_PY%" -c "import sys; print('  ' + sys.version.split()[0] + ' из ' + sys.executable)"
if errorlevel 1 goto :FAIL
echo.

REM ---------------------------------------------------------------------------
REM  3. Зависимости сборки
REM ---------------------------------------------------------------------------
echo [3/7] Зависимости:
"%VENV_PY%" -m pip install --upgrade pip --quiet
REM requirements-v2.txt — то же, что ставит start.bat: сборка обязана
REM использовать те же версии, что и запуск, иначе .exe поведёт себя иначе.
"%VENV_PY%" -m pip install -r requirements-v2.txt --quiet
if errorlevel 1 (
    echo   [ОШИБКА] Не удалось поставить runtime-зависимости.
    echo   Проверьте интернет и прокси.
    goto :FAIL
)

REM PyInstaller не входит в runtime: он нужен только для сборки, поэтому
REM ставится здесь, а не в requirements-v2.txt — иначе он поедет внутрь .exe.
"%VENV_PY%" -m pip install "pyinstaller>=6.0" --quiet
if errorlevel 1 (
    echo   [ОШИБКА] Не удалось поставить PyInstaller.
    goto :FAIL
)
"%VENV_PY%" -c "import PySide6, yt_dlp, imageio_ffmpeg; print('  зависимости на месте')"
if errorlevel 1 (
    echo   [ОШИБКА] Зависимости не импортируются после установки.
    goto :FAIL
)
"%VENV_PY%" -c "import PyInstaller; print('  PyInstaller ' + PyInstaller.__version__)"
echo.

REM ---------------------------------------------------------------------------
REM  4. Иконка
REM ---------------------------------------------------------------------------
echo [4/7] Иконка:
if exist "%ICON%" (
    echo   %ICON%
) else (
    echo   [ОШИБКА] Нет файла иконки: %ICON%
    echo   Без него .exe получит пустой значок Windows. Файл должен быть
    echo   в репозитории; если его нет — скачайте проект заново.
    goto :FAIL
)
echo.

REM ---------------------------------------------------------------------------
REM  5. Чистка прошлой сборки
REM ---------------------------------------------------------------------------
echo [5/7] Чистка:
REM Старые build\ и dist\ опасны не местом, а путаницей: PyInstaller
REM переиспользует промежуточные файлы, и в .exe может уехать модуль,
REM которого в коде уже нет. Сборка начинается с нуля.
REM build\ чистит сам PyInstaller по ключу --clean; здесь — только dist\,
REM потому что старый .exe иначе останется лежать рядом с новым, и легко
REM раздать не тот файл.
if exist "%OUT_DIR%" rmdir /s /q "%OUT_DIR%" 2>nul
echo   прошлый %OUT_DIR%\ удалён
echo.

REM ---------------------------------------------------------------------------
REM  6. Сборка
REM ---------------------------------------------------------------------------
echo [6/7] Сборка .exe (3–10 минут, это нормально):
echo.
"%VENV_PY%" -m PyInstaller tvhost.spec --noconfirm --clean
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
REM  7. Контрольные суммы и итог
REM ---------------------------------------------------------------------------
echo [7/7] Контрольные суммы:
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
REM Два пробела между хешем и именем — это формат «текстовый», не «двоичный»:
REM с одним пробелом часть инструментов считает файл бинарным.
> "%EXE%.md5"    echo !MD5!  %APP_NAME%.exe
> "%EXE%.sha256" echo !SHA!  %APP_NAME%.exe

echo   MD5:    !MD5!
echo   SHA256: !SHA!
echo   записаны в %EXE%.md5 и %EXE%.sha256
echo.

echo ============================================================
echo   Готово.
echo.
echo   Файлы для раздачи:
echo     %EXE%
echo     %EXE%.md5
echo     %EXE%.sha256
echo.
echo   Проверить перед раздачей:
echo     PowerShell:  Get-FileHash "%EXE%" -Algorithm SHA256
echo     Linux/macOS: sha256sum -c %APP_NAME%.exe.sha256
echo.
echo   Окружение сборки осталось в %BUILD_VENV%\ — его можно удалить,
echo   на работу приложения это не влияет. При следующей сборке оно
echo   создастся заново.
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
