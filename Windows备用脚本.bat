@echo off
:: 65001 导致了对控制台输入（stdin）和控制信号（如 Ctrl+C）的处理机制不正常响应的bug
:: chcp 65001 > nul
setlocal enabledelayedexpansion

echo ===================================
echo Welcome to Hajimi Auto Setup
echo ===================================

:: Load environment variables
echo Loading environment variables...
if exist ".env" (
    for /f "tokens=1,2 delims==" %%a in (.env) do (
        set "%%a=%%b"
        echo Loaded: %%a
    )
    echo Environment variables loaded successfully!
) else (
    echo Warning: .env file not found
)

:: Set variables
set PYTHON_VERSION=3.12.3
set PYTHON_URL=https://www.python.org/ftp/python/%PYTHON_VERSION%/python-%PYTHON_VERSION%-embed-amd64.zip
set PYTHON_ZIP=python-%PYTHON_VERSION%-embed-amd64.zip
set PYTHON_DIR=%~dp0python
set GET_PIP_URL=https://bootstrap.pypa.io/get-pip.py
set PATH=%PYTHON_DIR%;%PYTHON_DIR%\Scripts;%PATH%

:: Check if Python is already installed
if exist "%PYTHON_DIR%\python.exe" (
    echo Python already installed, skipping installation...
) else (
    echo Downloading Python %PYTHON_VERSION%...
    powershell -Command "Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%PYTHON_ZIP%'"
    
    echo Extracting Python...
    powershell -Command "Expand-Archive -Path '%PYTHON_ZIP%' -DestinationPath '%PYTHON_DIR%' -Force"
    
    echo Removing downloaded zip...
    del "%PYTHON_ZIP%"
    
    :: Modify python312._pth file to enable imports
    echo Configuring Python environment...
    powershell -Command "(Get-Content '%PYTHON_DIR%\python312._pth') -replace '#import site', 'import site' | Set-Content '%PYTHON_DIR%\python312._pth'"
    
    :: Download and install pip
    echo Downloading and installing pip...
    powershell -Command "Invoke-WebRequest -Uri '%GET_PIP_URL%' -OutFile '%PYTHON_DIR%\get-pip.py'"
    "%PYTHON_DIR%\python.exe" "%PYTHON_DIR%\get-pip.py" --no-warn-script-location
    
    echo Python and pip installation complete!
)

:: Check if dependencies need to be installed
if not exist "%~dp0.venv" (
    echo Creating virtual environment...
    "%PYTHON_DIR%\python.exe" -m venv "%~dp0.venv"
    
    echo Installing project dependencies...
    call "%~dp0.venv\Scripts\activate.bat"
    python -m pip install --upgrade pip
    
    echo Installing uv accelerator...
    pip install uv
    
    echo Using uv to install dependencies...
    python -m uv pip install -r requirements.txt
    
    echo Dependencies installation complete!
) else (
    echo Virtual environment exists, activating...
    call "%~dp0.venv\Scripts\activate.bat"
)

:: Start the application with delayed browser launch
echo Starting Hajimi application...
uvicorn app.main:app --host 0.0.0.0 --port 7860

endlocal
