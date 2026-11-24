@echo off
setlocal
cd /d "%~dp0"

rem Search for pythonw.exe or pyw.exe with full path
set "PY_BIN="

if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" (
    set "PY_BIN=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
) else (
    for /f "delims=" %%I in ('where pythonw.exe 2^>nul') do (
        if not defined PY_BIN set "PY_BIN=%%I"
    )
)

if not defined PY_BIN (
    if exist "%LOCALAPPDATA%\Programs\Python\Launcher\pyw.exe" (
        set "PY_BIN=%LOCALAPPDATA%\Programs\Python\Launcher\pyw.exe"
    ) else (
        for /f "delims=" %%I in ('where pyw.exe 2^>nul') do (
            if not defined PY_BIN set "PY_BIN=%%I"
        )
    )
)

if not defined PY_BIN (
    for /f "delims=" %%I in ('where python.exe 2^>nul') do (
        if not defined PY_BIN set "PY_BIN=%%I"
    )
)

if not defined PY_BIN (
    echo =======================================================
    echo [ERROR] No se encontro Python ni pythonw en el sistema.
    echo Asegurate de tener Python instalado y en el PATH.
    echo =======================================================
    pause
    exit /b 1
)

start "" "%PY_BIN%" "%~dp0run.py" %*
