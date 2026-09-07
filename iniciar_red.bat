@echo off
rem Inicia el conciliador accesible desde TODA la red local (no solo esta PC).
rem La primera vez, correr como administrador para abrir el puerto en el firewall:
rem   netsh advfirewall firewall add rule name="Conciliador 8765" dir=in action=allow protocol=TCP localport=8765
cd /d "%~dp0"
echo Instalando dependencias (solo la primera vez tarda)...
python -m pip install -r requirements.txt --quiet
set PORT=8765
echo.
echo Conciliador disponible en la red: http://%COMPUTERNAME%:8765/diario
start "" http://localhost:8765/diario
python app.py
