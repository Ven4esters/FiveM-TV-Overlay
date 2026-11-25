@echo off
REM ============================================================================
REM  FiveM Video Overlay - сборка .exe
REM
REM  dist\FiveM-Video-Overlay.exe  +  .md5  +  .sha256  +  .txt для README
REM
REM  Окружение сборки отдельное (build-venv\): собрать можно на чистой машине,
REM  даже если приложение ни разу не запускали через start.bat.
REM
REM  Нужен Python 3.10+ (64-бит) из python.org с "Add to PATH".
REM  Время: 3-10 минут; первый запуск дольше - ставится окружение.
REM
REM  КОДИРОВКА: файл в CP866 - родная страница консоли русской Windows.
REM  chcp 65001 здесь НЕТ намеренно: cmd.exe с UTF-8 в .bat нестабилен и
REM  может молча закрыть окно. Не пересохраняйте файл в UTF-8.
REM ============================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

if not defined PYTHONIOENCODING set "PYTHONIOENCODING=utf-8"
if not defined PYTHONUTF8 set "PYTHONUTF8=1"

title FiveM Video Overlay - сборка .exe

set "BUILD_VENV=build-venv"
set "VENV_PY=%BUILD_VENV%\Scripts\python.exe"
set "APP_NAME=FiveM-Video-Overlay"
set "OUT_DIR=dist"
set "EXE=%OUT_DIR%\%APP_NAME%.exe"
set "ICON=src\tvhost2\ui\assets\app.ico"
set "REPORT=%OUT_DIR%\%APP_NAME%.exe.txt"
set "LOG=build-log.txt"

REM Журнал: каждая строка уходит и в файл. Если окно закроется, видно шаг.
echo FiveM Video Overlay - журнал сборки> "%LOG%"
echo дата: %DATE% %TIME%>> "%LOG%"
echo.>> "%LOG%"

echo ============================================================
echo   FiveM Video Overlay - сборка .exe
echo ============================================================
echo   Журнал: %LOG%
echo ============================================================
echo.

REM ------------------------------------------------------------------ 1 / 7
echo [1/7] Проверяю Python...
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
    echo   Поставьте Python 3.12 с python.org, отметьте "Add python.exe to PATH"
    echo   и запустите build.bat снова. start.bat для сборки не нужен.
    goto :failed
)
%PYEXE% -c "import sys; sys.exit(0 if sys.maxsize > 2**32 else 1)"
if errorlevel 1 (
    echo   [ОШИБКА] Python 32-битный. Нужен 64-битный: PySide6 не собирается под 32 бита.
    goto :failed
)
for /f "delims=" %%V in ('%PYEXE% -c "import sys; print(sys.version.split()[0])"') do set "PY_VER=%%V"
echo   Python !PY_VER!
echo.

REM ------------------------------------------------------------------ 2 / 7
echo [2/7] Окружение сборки %BUILD_VENV%\...
if exist "%VENV_PY%" goto :venv_ready
echo   создаю, это занимает до минуты...
%PYEXE% -m venv "%BUILD_VENV%"
if errorlevel 1 (
    echo   [ОШИБКА] Не удалось создать %BUILD_VENV%. Проверьте права и место на диске.
    goto :failed
)
if not exist "%VENV_PY%" (
    echo   [ОШИБКА] venv создан, но %VENV_PY% не появился.
    echo   Обычно так бывает с Python из Microsoft Store - он создаёт неполные
    echo   окружения. Поставьте Python с python.org.
    goto :failed
)
:venv_ready
echo   готово
echo.

REM ------------------------------------------------------------------ 3 / 7
echo [3/7] Ставлю зависимости...
"%VENV_PY%" -m pip install --upgrade pip --quiet
"%VENV_PY%" -m pip install -r requirements-v2.txt --quiet
if errorlevel 1 (
    echo   [ОШИБКА] Не удалось поставить зависимости. Проверьте интернет и прокси.
    goto :failed
)
REM PyInstaller нужен только для сборки: в requirements его нет намеренно,
REM иначе он поедет внутрь .exe.
"%VENV_PY%" -m pip install "pyinstaller>=6.0" --quiet
if errorlevel 1 (
    echo   [ОШИБКА] Не удалось поставить PyInstaller.
    goto :failed
)
"%VENV_PY%" -c "import PySide6, yt_dlp, imageio_ffmpeg; print('   зависимости на месте')"
if errorlevel 1 goto :failed
"%VENV_PY%" -c "import PyInstaller; print('   PyInstaller ' + PyInstaller.__version__)"
echo.

REM ------------------------------------------------------------------ 4 / 7
echo [4/7] Проверяю иконку...
if not exist "%ICON%" (
    echo   [ОШИБКА] Нет файла иконки: %ICON%
    echo   Без него .exe получит пустой значок Windows.
    goto :failed
)
echo   %ICON%
echo.

REM ------------------------------------------------------------------ 5 / 7
echo [5/7] Чищу прошлую сборку...
REM Старый dist\ опасен не местом, а путаницей: легко раздать не тот файл.
if exist "%OUT_DIR%" rmdir /s /q "%OUT_DIR%" 2>nul
echo   прошлый %OUT_DIR%\ удалён
echo.

REM ------------------------------------------------------------------ 6 / 7
echo [6/7] Собираю .exe (3-10 минут, это нормально)...
echo.
"%VENV_PY%" -m PyInstaller tvhost.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo   [ОШИБКА] PyInstaller завершился с ошибкой.
    echo   Журнал PyInstaller: build\%APP_NAME%\warn-%APP_NAME%.txt
    goto :failed
)
echo.
if not exist "%EXE%" (
    echo   [ОШИБКА] Сборка прошла, но %EXE% не появился.
    goto :failed
)
echo   готово
echo.

REM ------------------------------------------------------------------ 7 / 7
echo [7/7] Считаю контрольные суммы и готовлю данные для README...
REM certutil печатает хеш ЗАГЛАВНЫМИ, а findstr без /i его не находит:
REM MD5 остался бы пустым, и сборка упала бы на ровном месте. Проверено.
set "MD5="
set "SHA="
for /f "delims=" %%L in ('certutil -hashfile "%EXE%" MD5 ^| findstr /r /i "^[0-9a-f][0-9a-f]*$"') do if not defined MD5 set "MD5=%%L"
for /f "delims=" %%L in ('certutil -hashfile "%EXE%" SHA256 ^| findstr /r /i "^[0-9a-f][0-9a-f]*$"') do if not defined SHA set "SHA=%%L"
if defined MD5  set "MD5=!MD5:A=a!" & set "MD5=!MD5:B=b!" & set "MD5=!MD5:C=c!" & set "MD5=!MD5:D=d!" & set "MD5=!MD5:E=e!" & set "MD5=!MD5:F=f!"
if defined SHA  set "SHA=!SHA:A=a!" & set "SHA=!SHA:B=b!" & set "SHA=!SHA:C=c!" & set "SHA=!SHA:D=d!" & set "SHA=!SHA:E=e!" & set "SHA=!SHA:F=f!"
if not defined MD5 (
    echo   [ОШИБКА] Не удалось посчитать MD5 - certutil недоступен?
    goto :failed
)
if not defined SHA (
    echo   [ОШИБКА] Не удалось посчитать SHA256.
    goto :failed
)

REM Два пробела между хешем и именем - "текстовый" формат: его понимают
REM md5sum -c, sha256sum -c и Get-FileHash.
> "%EXE%.md5"    echo !MD5!  %APP_NAME%.exe
> "%EXE%.sha256" echo !SHA!  %APP_NAME%.exe

REM Размер файла и папки для отчёта.
for %%F in ("%EXE%") do set "SIZE_BYTES=%%~zF"
set /a SIZE_MB=!SIZE_BYTES! / 1048576
set /a SIZE_KB=!SIZE_BYTES! / 1024

REM Версия берётся из pyproject.toml, а не вписывается руками: иначе однажды
REM в README окажется 2.1.0, а в файле 2.2.0, и никто не заметит.
set "VERSION="
for /f "tokens=2 delims== " %%V in ('findstr /b /c:"version" pyproject.toml') do if not defined VERSION set "VERSION=%%~V"
if not defined VERSION set "VERSION=?"

REM ---------------------------------------------------------------------------
REM  Отчёт для README: копируется в таблицу "Контрольные суммы" как есть.
REM  Кодировка отчёта - системная ANSI (CP866 на русской Windows):
REM  её понимают Блокнот и сам cmd без дополнительных настроек. Если
REM  открываете в VS Code - выберите кодировку CP866, иначе увидите
REM  кракозябры. Значения хешей от кодировки не зависят: это ASCII.
REM ---------------------------------------------------------------------------
REM Блок (...) здесь не используется намеренно: "echo." внутри скобок
REM в cmd срабатывает не всегда, а скобки в тексте пришлось бы экранировать.
REM Перед записью файл обнуляется: иначе останется хвост от прошлой сборки.
type nul > "%REPORT%"
>> "%REPORT%" echo FiveM Video Overlay !VERSION! - данные сборки
>> "%REPORT%" echo.
>> "%REPORT%" echo Эти строки вставляются в README.md, раздел Проверка файла.
>> "%REPORT%" echo Файл создан build.bat !DATE! !TIME!
>> "%REPORT%" echo.
>> "%REPORT%" echo --- для таблицы в README ---------------------------------------------
>> "%REPORT%" echo Версия:        !VERSION!
>> "%REPORT%" echo Файл:          %APP_NAME%.exe
>> "%REPORT%" echo Размер файла:  !SIZE_BYTES! байт (!SIZE_MB! МБ)
>> "%REPORT%" echo Дата сборки:   !DATE!
>> "%REPORT%" echo MD5:           !MD5!
>> "%REPORT%" echo SHA-256:       !SHA!
>> "%REPORT%" echo.
>> "%REPORT%" echo --- команды проверки ------------------------------------------------
>> "%REPORT%" echo PowerShell:    Get-FileHash .\%APP_NAME%.exe -Algorithm SHA256
>> "%REPORT%" echo cmd:           certutil -hashfile %APP_NAME%.exe SHA256
>> "%REPORT%" echo Linux/macOS:   sha256sum -c %APP_NAME%.exe.sha256
>> "%REPORT%" echo.
>> "%REPORT%" echo --- окружение, в котором собрано -----------------------------------
>> "%REPORT%" echo Python:        !PY_VER! (64-бит)
>> "%REPORT%" echo PyInstaller:   см. build-log.txt
>> "%REPORT%" echo.
>> "%REPORT%" echo --- файлы на выходе ------------------------------------------------
>> "%REPORT%" echo %EXE%
>> "%REPORT%" echo %EXE%.md5
>> "%REPORT%" echo %EXE%.sha256
>> "%REPORT%" echo %REPORT%

echo   MD5:    !MD5!
echo   SHA256: !SHA!
echo   отчёт:  %REPORT%
echo.

echo ============================================================
echo   ГОТОВО
echo.
echo   dist\%APP_NAME%.exe            - сама программа
echo   dist\%APP_NAME%.exe.md5        - контрольная сумма
echo   dist\%APP_NAME%.exe.sha256     - контрольная сумма
echo   dist\%APP_NAME%.exe.txt        - данные для README
echo.
echo   Вставьте строки из .txt в README.md, раздел Проверка файла.
echo   Окружение сборки: %BUILD_VENV%\ - можно удалить.
echo ============================================================
>>"%LOG%" echo.
>>"%LOG%" echo ============ ГОТОВО: %EXE% (!SIZE_MB! МБ) ============
>>"%LOG%" echo MD5:    !MD5!
>>"%LOG%" echo SHA256: !SHA!
echo.
pause
exit /b 0

:failed
echo.
echo ============================================================
echo   СБОРКА НЕ УДАЛАСЬ - .exe не создан.
echo   Причина выше. Журнал целиком: %LOG%
echo   Пришлите этот файл, если причина не ясна.
echo ============================================================
>>"%LOG%" echo.
>>"%LOG%" echo ============ СБОРКА НЕ УДАЛАСЬ ============
echo.
pause
exit /b 1
