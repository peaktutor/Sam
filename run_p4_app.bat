@echo off
rem Starts the P4 Analysis window. Double-click this file.
rem It uses the first Python it finds that has what the window needs (tkinter, pandas, matplotlib, scipy):
rem   1. .venv-vbn here (the real-data environment with AllenSDK)
rem   2. a conda environment called vbn
rem   3. .venv-test here (demo data only)
rem   4. the Python launcher (pyw)
setlocal
cd /d "%~dp0"

for %%P in (
    "%~dp0.venv-vbn\Scripts\pythonw.exe"
    "%USERPROFILE%\miniconda3\envs\vbn\pythonw.exe"
    "%USERPROFILE%\anaconda3\envs\vbn\pythonw.exe"
    "%LOCALAPPDATA%\miniconda3\envs\vbn\pythonw.exe"
    "%~dp0.venv-test\Scripts\pythonw.exe"
) do (
    if exist %%P (
        "%%~P" -c "import tkinter, pandas, matplotlib, scipy" 2>nul && (
            start "" "%%~P" -m p4_app %*
            exit /b 0
        )
    )
)

where pyw >nul 2>nul && (
    start "" pyw -m p4_app %*
    exit /b 0
)

echo Could not find a Python with tkinter, pandas, matplotlib and scipy.
echo See README.md, "Setting up", for the commands that create one.
pause
exit /b 1
