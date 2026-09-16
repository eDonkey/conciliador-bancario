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

echo.
echo Conciliador (rama fbs-sql) en http://%COMPUTERNAME%:8766/diario
echo Con AUTORECARGA: despues de un "git pull" el server se recarga solo
echo (solo si el pull trae dependencias nuevas hay que volver a correr este .bat).
start "" http://localhost:8766/diario
rem --reload vigila solo los .py (los json de datos/ no disparan recargas).
rem Ojo: --reload-exclude con globs no sirve en Windows (Click los expande).
python -m uvicorn app:app --host 0.0.0.0 --port 8766 --reload
