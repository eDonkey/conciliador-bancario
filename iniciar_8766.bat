@echo off
rem TEMPORAL: baja lo que este escuchando en el puerto 8765 (el conciliador
rem viejo) y levanta ESTA copia (rama fbs-sql) en el puerto 8766.
cd /d "%~dp0"

echo Bajando lo que este escuchando en el puerto 8765...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8765 " ^| findstr LISTENING') do (
    echo   matando proceso %%p
    taskkill /f /pid %%p >nul 2>&1
)

echo Bajando lo que ya este en el 8766 (por si quedo colgado)...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8766 " ^| findstr LISTENING') do (
    taskkill /f /pid %%p >nul 2>&1
)

echo Instalando dependencias (solo la primera vez tarda)...
python -m pip install -r requirements.txt --quiet

set PORT=8766
echo.
echo Conciliador (rama fbs-sql) en http://%COMPUTERNAME%:8766/diario
start "" http://localhost:8766/diario
python app.py
