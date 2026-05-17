@echo off
REM Quick launcher for development. Run from the FD6\ directory.
setlocal
cd /d "%~dp0"
python -m fd6
endlocal
