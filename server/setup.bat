@echo off
cd /d "%~dp0"
set "ENV_NAME="
echo Please enter a conda environment name (default: lty):
set /p ENV_NAME=
if not defined ENV_NAME set "ENV_NAME=lty"

echo Creating environment: %ENV_NAME%
call conda create -n %ENV_NAME% python=3.10 -y
call conda activate %ENV_NAME%

set "INSTALL_CUDA="
echo Please select CUDA version (1: CUDA 13.0, 2: CUDA 12.8, 3: CUDA 12.6, 4: No CUDA, default: 4):
set /p INSTALL_CUDA=
if not defined INSTALL_CUDA set "INSTALL_CUDA=4"

if "%INSTALL_CUDA%"=="1" (
    echo Installing PyTorch with CUDA 13.0 support...
    pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu130
) else if "%INSTALL_CUDA%"=="2" (
    echo Installing PyTorch with CUDA 12.8 support...
    pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
) else if "%INSTALL_CUDA%"=="3" (
    echo Installing PyTorch with CUDA 12.6 support...
    pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126
) else (
    echo Not installing PyTorch with CUDA support, PyTorch will be installed later.
)

call conda install ffmpeg -y
pip install -r docs/gsv_requirements.txt
pip install -r docs/requirements.txt

REM Install the standalone MultiSpeakerTTS distribution shipped with this repo.
set "GSV_WHEEL="
for %%f in (res\packages\gsv_tts_lite_multispeaker-*.whl) do set "GSV_WHEEL=%%f"
if defined GSV_WHEEL (
    echo Removing the incompatible upstream gsv-tts-lite distribution...
    pip uninstall -y gsv-tts-lite >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Failed to remove the upstream gsv-tts-lite distribution.
        exit /b 1
    )
    echo Installing gsv-tts-lite-multispeaker from %GSV_WHEEL%
    pip install --force-reinstall "%GSV_WHEEL%"
    if errorlevel 1 (
        echo [ERROR] Failed to install %GSV_WHEEL%.
        exit /b 1
    )
) else (
    echo [ERROR] No gsv-tts-lite-multispeaker wheel found in res\packages.
    exit /b 1
)

python -m playwright install chromium
pause
