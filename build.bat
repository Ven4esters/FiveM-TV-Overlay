@echo off
REM ============================================================================
REM  FiveM Video Overlay - сборка .exe
REM
REM  Результат: dist\FiveM-Video-Overlay.exe, .exe.md5, .exe.sha256
REM
REM  Окружение сборки отдельное: build-venv\. Оно не зависит от .venv,
REM  которым пользуется start.bat, и создаётся само. Собрать .exe можно
REM  на чистой машине, даже если приложение ни разу не запускали.
REM
REM  Что нужно: Python 3.10+ (64-бит) из python.org с "Add to PATH".
REM  Время: 3-10 минут; первый запуск дольше - ставится окружение.
REM
REM  КОДИРОВКА: файл сохранён в CP866 - это кодовая страница консоли русской
REM  Windows. chcp 65001 здесь НЕТ намеренно: cmd.exe с UTF-8 в .bat
REM  работает нестабильно и может молча закрыть окно на первой строке.
REM  Не пересохраняйте файл в UTF-8 - кириллица станет кракозябрами.
REM ============================================================================
setlocal EnableExtensions EnableDelayedExpansion

if not defined PYTHONIOENCODING set "PYTHONIOENCODING=utf-8"
if not defined PYTHONUTF8 set "PYTHONUTF8=1"

cd /d "%~dp0"
title FiveM Video Overlay - сборка .exe

set "BUILD_VENV=build-venv"
set "VENV_PY=%BUILD_VENV%\Scripts\python.exe"
set "APP_NAME=FiveM-Video-Overlay"
set "OUT_DIR=dist"
set "EXE=%OUT_DIR%\%APP_NAME%.exe"
set "ICON=src\tvhost2\ui\assets\app.ico"
set "LOG=build-log.txt"

REM Журнал создаётся сразу: если его нет после запуска, значит скрипт не
REM смог начать вообще, и причина в самом файле, а не в Python.
echo FiveM Video Overlay - журнал сборки> "%LOG%"
echo дата: %DATE% %TIME%>> "%LOG%"
echo.>> "%LOG%"

echo ============================================================
echo   FiveM Video Overlay - сборка .exe
echo ============================================================
echo   Журнал: %LOG%
echo ============================================================
echo.

REM ===========================================================================
REM  1. Python
REM ===========================================================================
set "SAY=[1/7] Python:"
call :SAY

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
    set "SAY=  [ОШИБКА] Python не найден."
    call :SAY
    set "SAY=  Поставьте Python 3.12 с python.org, отметьте"
    call :SAY
    set "SAY=  Add python.exe to PATH и запустите build.bat снова."
    call :SAY
    goto :FAIL
)

REM PySide6 не имеет 32-битных сборок: такой .exe не запустится ни у кого.
REM Проверка идёт ДО создания окружения, чтобы не тратить десять минут.
%PYEXE% -c "import sys; sys.exit(0 if sys.maxsize > 2**32 else 1)"
if errorlevel 1 (
    set "SAY=  [ОШИБКА] Python 32-битный. Нужен 64-битный."
    call :SAY
    goto :FAIL
)

for /f "delims=" %%V in ('%PYEXE% -c "import sys; print(sys.version.split()[0])"') do set "BASE_VER=%%V"
set "SAY=  базовый Python: !BASE_VER!"
call :SAY
call :BLANK

REM ===========================================================================
REM  2. Окружение сборки
REM ===========================================================================
set "SAY=[2/7] Окружение сборки %BUILD_VENV%\:"
call :SAY
if exist "%VENV_PY%" (
    set "SAY=  уже есть - переиспользую"
    call :SAY
) else (
    set "SAY=  создаю, это занимает до минуты..."
    call :SAY
    %PYEXE% -m venv "%BUILD_VENV%"
    if errorlevel 1 (
        set "SAY=  [ОШИБКА] Не удалось создать %BUILD_VENV%. Проверьте права и место на диске."
        call :SAY
        goto :FAIL
    )
    if not exist "%VENV_PY%" (
        set "SAY=  [ОШИБКА] venv создан, но %VENV_PY% не появился."
        call :SAY
        set "SAY=  Обычно так бывает с Python из Microsoft Store: он создаёт"
        call :SAY
        set "SAY=  неполные окружения. Поставьте Python с python.org."
        call :SAY
        goto :FAIL
    )
    set "SAY=  готово"
    call :SAY
)
"%VENV_PY%" -c "import sys; print('  Python ' + sys.version.split()[0])"
if errorlevel 1 (
    set "SAY=  [ОШИБКА] Python внутри %BUILD_VENV% не запускается."
    call :SAY
    goto :FAIL
)
call :BLANK

REM ===========================================================================
REM  3. Зависимости
REM ===========================================================================
set "SAY=[3/7] Зависимости:"
call :SAY
"%VENV_PY%" -m pip install --upgrade pip --quiet
"%VENV_PY%" -m pip install -r requirements-v2.txt --quiet
if errorlevel 1 (
    set "SAY=  [ОШИБКА] Не удалось поставить runtime-зависимости."
    call :SAY
    set "SAY=  Проверьте интернет и прокси."
    call :SAY
    goto :FAIL
)

REM PyInstaller нужен только для сборки: в requirements его нет намеренно,
REM иначе он поедет внутрь .exe.
"%VENV_PY%" -m pip install "pyinstaller>=6.0" --quiet
if errorlevel 1 (
    set "SAY=  [ОШИБКА] Не удалось поставить PyInstaller."
    call :SAY
    goto :FAIL
)
"%VENV_PY%" -c "import PySide6, yt_dlp, imageio_ffmpeg; print('  зависимости на месте')"
if errorlevel 1 (
    set "SAY=  [ОШИБКА] Зависимости не импортируются после установки."
    call :SAY
    goto :FAIL
)
"%VENV_PY%" -c "import PyInstaller; print('  PyInstaller ' + PyInstaller.__version__)"
call :BLANK

REM ===========================================================================
REM  4. Иконка
REM ===========================================================================
set "SAY=[4/7] Иконка:"
call :SAY
if exist "%ICON%" (
    set "SAY=  %ICON%"
    call :SAY
) else (
    set "SAY=  [ОШИБКА] Нет файла иконки: %ICON%"
    call :SAY
    set "SAY=  Без него .exe получит пустой значок Windows."
    call :SAY
    goto :FAIL
)
call :BLANK

REM ===========================================================================
REM  5. Чистка
REM ===========================================================================
set "SAY=[5/7] Чистка:"
call :SAY
REM Старый dist\ опасен не местом, а путаницей: легко раздать не тот файл.
if exist "%OUT_DIR%" rmdir /s /q "%OUT_DIR%" 2>nul
set "SAY=  прошлый %OUT_DIR%\ удалён"
call :SAY
call :BLANK

REM ===========================================================================
REM  6. Сборка
REM ===========================================================================
set "SAY=[6/7] Сборка .exe (3-10 минут, это нормально):"
call :SAY
echo.
"%VENV_PY%" -m PyInstaller tvhost.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    set "SAY=  [ОШИБКА] PyInstaller завершился с ошибкой."
    call :SAY
    set "SAY=  Журнал PyInstaller: build\%APP_NAME%\warn-%APP_NAME%.txt"
    call :SAY
    goto :FAIL
)
echo.
if not exist "%EXE%" (
    set "SAY=  [ОШИБКА] Сборка прошла, но %EXE% не появился."
    call :SAY
    goto :FAIL
)

for %%F in ("%EXE%") do set "SIZE=%%~zF"
set /a SIZE_MB=!SIZE! / 1048576
set "SAY=  готово: %EXE%  (!SIZE_MB! МБ)"
call :SAY
call :BLANK

REM ===========================================================================
REM  7. Контрольные суммы
REM ===========================================================================
set "SAY=[7/7] Контрольные суммы:"
call :SAY
REM certutil печатает хеш ЗАГЛАВНЫМИ, а findstr без /i его не находит:
REM MD5 остаётся пустым, и сборка падает на ровном месте. Проверено.
set "MD5="
set "SHA="
for /f "delims=" %%L in ('certutil -hashfile "%EXE%" MD5 ^| findstr /r /i "^[0-9a-f][0-9a-f]*$"') do if not defined MD5 set "MD5=%%L"
for /f "delims=" %%L in ('certutil -hashfile "%EXE%" SHA256 ^| findstr /r /i "^[0-9a-f][0-9a-f]*$"') do if not defined SHA set "SHA=%%L"

if defined MD5  set "MD5=!MD5:A=a!" & set "MD5=!MD5:B=b!" & set "MD5=!MD5:C=c!" & set "MD5=!MD5:D=d!" & set "MD5=!MD5:E=e!" & set "MD5=!MD5:F=f!"
if defined SHA  set "SHA=!SHA:A=a!" & set "SHA=!SHA:B=b!" & set "SHA=!SHA:C=c!" & set "SHA=!SHA:D=d!" & set "SHA=!SHA:E=e!" & set "SHA=!SHA:F=f!"

if not defined MD5 (
    set "SAY=  [ОШИБКА] Не удалось посчитать MD5 - certutil недоступен?"
    call :SAY
    goto :FAIL
)
if not defined SHA (
    set "SAY=  [ОШИБКА] Не удалось посчитать SHA256."
    call :SAY
    goto :FAIL
)

REM Два пробела между хешем и именем - "текстовый" формат: его понимают
REM md5sum -c, sha256sum -c и Get-FileHash.
> "%EXE%.md5"    echo !MD5!  %APP_NAME%.exe
> "%EXE%.sha256" echo !SHA!  %APP_NAME%.exe

set "SAY=  MD5:    !MD5!"
call :SAY
set "SAY=  SHA256: !SHA!"
call :SAY
set "SAY=  записаны в %EXE%.md5 и %EXE%.sha256"
call :SAY
call :BLANK

echo ============================================================
echo   ГОТОВО
echo.
echo   Файлы для раздачи:
echo     %EXE%
echo     %EXE%.md5
echo     %EXE%.sha256
echo.
echo   Проверить:  Get-FileHash "%EXE%" -Algorithm SHA256
echo.
echo   Окружение сборки: %BUILD_VENV%\ - можно удалить,
echo   на работу приложения это не влияет.
echo ============================================================
>>"%LOG%" echo.
>>"%LOG%" echo ============================================================
>>"%LOG%" echo   ГОТОВО
>>"%LOG%" echo   Файлы: %EXE%, .md5, .sha256
>>"%LOG%" echo ============================================================
echo.
pause
exit /b 0

REM ===========================================================================
REM  Подпрограммы
REM ===========================================================================
:SAY
REM Печатает строку из SAY на экран и в журнал.
REM Почему через переменную, а не через %*: парсер cmd съедает одиночный %
REM в аргументах ещё до вызова, и в журнал попадало бы искажённое значение.
REM Значение SAY уже развёрнуто на строке set, поэтому здесь потерь нет.
echo !SAY!
>>"%LOG%" echo !SAY!
exit /b 0

:BLANK
echo.
>>"%LOG%" echo.
exit /b 0

:FAIL
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
