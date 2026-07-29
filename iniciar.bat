@echo off
cd /d "%~dp0"
echo Instalando dependencias (solo la primera vez tarda)...
python -m pip install -r requirements.txt --quiet
echo.
echo Iniciando el conciliador en http://localhost:8765
start "" http://localhost:8765
python app.py
