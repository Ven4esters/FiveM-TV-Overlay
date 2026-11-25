@echo off
REM ============================================================================
REM ---------------------------------------------------------------------------
REM  Кодировка: файл сохранён в CP866 (кодовая страница
REM  консоли русской Windows) и не переключает её через chcp 65001.
REM  С UTF-8 в .bat cmd.exe работает нестабильно - скрипт может молча
REM  закрыться. Не пересохраняйте файл в UTF-8.
REM ---------------------------------------------------------------------------
REM  FiveM Video Overlay 2.0 - единый запуск для Windows 10/11
REM
REM  Первый запуск:  создаёт окружение, ставит зависимости, запускает.
REM  Следующие:      сразу запускает.
REM
REM ============================================================================
setlocal EnableExtensions EnableDelayedExpansion

REM Переменные - ПОСЛЕ setlocal, иначе теряются.
if not defined PYTHONIOENCODING set "PYTHONIOENCODING=utf-8"
if not defined PYTHONUTF8 set "PYTHONUTF8=1"
if not defined PYTHONUNBUFFERED set "PYTHONUNBUFFERED=1"

cd /d "%~dp0"
title FiveM Video Overlay 2.0

set "VENV_PY=.venv\Scripts\python.exe"
set "VENV_PYW=.venv\Scripts\pythonw.exe"
set "PORT=9001"

REM ============================================================================
REM  1. Окружение готово?
REM ============================================================================
if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import yt_dlp, PySide6" >nul 2>&1
    if not errorlevel 1 goto RUN
    echo Окружение найдено, но зависимости неполные. Доустанавливаю...
    "%VENV_PY%" -m pip install -r requirements-v2.txt --quiet
    if errorlevel 1 goto FAIL
    goto RUN
)

REM ============================================================================
REM  2. Первый запуск - установка
REM ============================================================================
echo ============================================================
echo   FiveM Video Overlay 2.0 - первый запуск
echo ============================================================
echo.

REM --- Python ---
set "PYTHON="
where py >nul 2>&1
if not errorlevel 1 set "PYTHON=py -3"
if not defined PYTHON (
    where python >nul 2>&1
    if not errorlevel 1 set "PYTHON=python"
)
if not defined PYTHON goto NO_PYTHON

echo [1/4] Python:
%PYTHON% --version
if errorlevel 1 goto NO_PYTHON
echo.

REM Проверяем разрядность: 32-битный Python не потянет PySide6 и видеопоток.
%PYTHON% -c "import struct,sys; sys.exit(0 if struct.calcsize('P')*8 == 64 else 1)"
if errorlevel 1 (
    echo [ОШИБКА] Установлен 32-битный Python. Нужен 64-битный.
    echo Скачайте с python.org: на странице загрузок выберите
    echo "Windows installer (64-bit)".
    goto FAIL
)

REM --- окружение ---
echo [2/4] Создаю окружение .venv ...
%PYTHON% -m venv .venv
if errorlevel 1 goto FAIL
echo.

REM --- зависимости ---
echo [3/4] Устанавливаю зависимости. Нужен интернет, 1-3 минуты.
"%VENV_PY%" -m pip install --upgrade pip --quiet
"%VENV_PY%" -m pip install -r requirements-v2.txt
if errorlevel 1 goto FAIL
echo.

REM --- проверка ---
echo [4/4] Проверяю установку ...
"%VENV_PY%" -c "import yt_dlp, PySide6; print('   yt-dlp', yt_dlp.version.__version__); print('   PySide6', PySide6.__version__)"
if errorlevel 1 goto FAIL

REM ffmpeg нужен только для выравнивания громкости и склейки звука.
REM Без него приложение работает - поэтому это предупреждение, а не ошибка.
"%VENV_PY%" -c "import imageio_ffmpeg" >nul 2>&1
if errorlevel 1 (
    echo    [предупреждение] imageio-ffmpeg не установился.
    echo    Громкость и склейка звука будут недоступны, остальное работает.
) else (
    echo    ffmpeg есть - громкость и склейка доступны
)
echo.
echo Установка завершена.
timeout /t 2 >nul
echo.

REM ============================================================================
REM  3. Запуск
REM ============================================================================
:RUN
set "PYTHONPATH=%~dp0src"

REM Проверка: не занят ли порт прошлым запуском. Иначе окно откроется,
REM а прокси не поднимется - заказчик увидит "прокси не отвечает" и не поймёт.
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo Порт %PORT% уже занят - похоже, приложение уже запущено.
    echo Если это не так, закройте старое окно и запустите снова.
    echo.
)

echo Запускаю FiveM Video Overlay 2.0 ...
echo Прокси: http://127.0.0.1:%PORT%
echo Окно пульта откроется через пару секунд.
echo.
echo Это окно можно закрыть только вместе с приложением.
echo Чтобы остановить - закройте окно пульта.
echo.

REM python.exe, а не pythonw.exe: если приложение упадёт при старте,
REM сообщение будет видно в консоли. С pythonw окно просто не откроется,
REM и причина останется неизвестной.
"%VENV_PY%" -m tvhost2.ui --serve --port %PORT%
if errorlevel 1 goto CRASH
goto END

:NO_PYTHON
echo.
echo [ОШИБКА] Python не найден.
echo.
echo Что делать:
echo   1. Откройте https://www.python.org/downloads/
echo   2. Скачайте Python 3.12 (Windows installer 64-bit)
echo   3. При установке ОБЯЗАТЕЛЬНО отметьте
echo      "Add python.exe to PATH"
echo   4. Запустите start.bat снова
echo.
pause
exit /b 1

:CRASH
echo.
echo [ОШИБКА] Приложение завершилось с ошибкой.
echo.
echo Что можно попробовать:
echo   - Запустите ещё раз: часто помогает при первом старте
echo   - Проверьте, что установлен 64-битный Python 3.12
echo   - Пришлите разработчику последние строки выше
echo.
pause
exit /b 1

:FAIL
echo.
echo [ОШИБКА] Установка не завершилась.
echo Проверьте интернет-соединение и запустите start.bat снова.
echo Если не помогло - удалите папку .venv и запустите ещё раз.
echo.
pause
exit /b 1

:END
endlocal
exit /b 0
