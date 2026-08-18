@echo off
REM Spusteni aplikace na Windows.
REM Skript pri prvnim spusteni zalozi virtualni prostredi a doinstaluje zavislosti.
REM Pripadne prepinace se predavaji aplikaci, napriklad: run.bat --no-connect
setlocal
cd /d "%~dp0"

set VENV=.venv

if not exist "%VENV%" (
    REM Na Windows se interpret jmenuje python, pripadne je dostupny pres launcher py
    where python >nul 2>&1
    if %ERRORLEVEL%==0 (
        set PYTHON=python
    ) else (
        where py >nul 2>&1
        if %ERRORLEVEL%==0 (
            set PYTHON=py -3
        ) else (
            echo Nenalezen Python. Nainstalujte jej z https://www.python.org/downloads/
            exit /b 1
        )
    )

    echo Zakladam virtualni prostredi ...
    %PYTHON% -m venv "%VENV%"
    "%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip
    echo Instaluji zavislosti ...
    "%VENV%\Scripts\pip.exe" install --quiet -r requirements.txt
)

"%VENV%\Scripts\python.exe" main.py %*
endlocal
