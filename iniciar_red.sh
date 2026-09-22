#!/usr/bin/env bash
# Version Linux de iniciar_red.bat: inicia el conciliador accesible desde
# TODA la red local (no solo esta maquina), no solo localhost.
# La primera vez, si hay firewall (ufw) activo, abrir el puerto:
#   sudo ufw allow 8765/tcp comment "Conciliador"
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo "Creando entorno virtual (.venv)..."
    python3 -m venv .venv
fi

echo "Instalando dependencias (solo la primera vez tarda)..."
.venv/bin/pip install -r requirements.txt --quiet

export PORT=8765
echo ""
echo "Conciliador disponible en la red: http://$(hostname):8765/diario"
if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://localhost:8765/diario" >/dev/null 2>&1 &
fi
.venv/bin/python app.py
