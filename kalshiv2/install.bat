@echo off
echo ==========================================
echo   Installing KalshiV2 Dependencies
echo ==========================================
echo.

pip install -r "%~dp0requirements.txt"

echo.
echo ==========================================
echo   Installation Complete
echo ==========================================
echo.
echo To run the bot:
echo   cd %~dp0..
echo   python -m kalshiv2 status
echo   python -m kalshiv2 --demo
echo   python -m kalshiv2 --dry-run
echo.
pause
