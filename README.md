# Conciliador bancario

Aplicación web que cruza los extractos bancarios del grupo Orbis / Nave contra el libro mayor del FBS, su sistema de gestión, y clasifica lo que no cruza. La usa la administración del grupo, por marca. Es parte de Orbit.

Cada comando de este archivo se probó en un clon limpio el 2026-09-16, con Python 3.13 en un entorno virtual: la instalación completó y `python app.py` respondió 200 en `/` y en `/diario`.

El servicio de producción no pide ninguna credencial: la clave de acceso se quitó el 2026-07-29 y no se reemplazó [`6803948`; `docs/decisiones/0006-quitar-la-clave-de-acceso.md`].

## El modelo contable, en cinco líneas

El FBS registra cada movimiento en dos cuentas. La O es la operativa: órdenes de pago y recibos entran ahí, pendientes de confirmación. Al confirmarse se hace la contrapartida en la O y el asiento pasa a la E, que debería reflejar el extracto. Conciliar es cruzar el extracto contra la E; lo que no está en la E se busca en la O pendiente [`README.md:8-26`; `engine/matcher.py:4-13`].

## Los dos modos

**Mensual** (`/`). Se suben los extractos del período y el Excel del mayor, y se concilia de una vez. Acepta los formatos PDF, XLS, XLSX y CSV de cinco bancos (Santander, BBVA, Galicia, Macro, Ciudad) [`f53f7e8`].

**Diario multibanco** (`/diario`). Un tablero por cuenta bancaria. Lo que queda pendiente un día se arrastra al siguiente, la memoria evita que un movimiento se consuma dos veces, y el ciclo O→E confirma contra el mayor de hoy lo que ayer cruzó contra la O [`efee321`; `app.py:851-920`].

## Las dos ramas

`master` es lo que corre en Railway. El mayor entra como archivo exportado del FBS.

`origin/fbs-sql` lee el mayor directo del SQL Server del FBS, con protección de solo lectura en tres capas, y agrega las variables `FBS_SQL_*` y el módulo `engine/fbs_sql.py` [`76f791e`; `1e4a143`]. Tiene 31 commits que no están en `master` y corre on-premise en una máquina Windows del cliente, en el puerto 8766 [`b559d13`]. Antes de tocar cualquiera de las dos, mirá si el cambio ya existe en la otra: cinco arreglos se aplicaron dos veces [`git log --all`]. Ver `docs/decisiones/0001-leer-el-fbs-por-sql-server.md`.

## Stack

| Qué | Versión | Para qué |
|---|---|---|
| Python | 3.12 en el `Dockerfile`, 3.11+ según el README anterior | Todo el backend |
| `fastapi` | `>=0.110` | Servidor y las 34 rutas |
| `uvicorn` | `>=0.29` | ASGI |
| `python-multipart` | `>=0.0.9` | Subida de archivos |
| `pdfplumber` | `>=0.11` | Extracto de Santander en PDF |
| `openpyxl` | `>=3.1` | Mayor en XLSX, extractos de Galicia y Santander, Excel de salida |
| `xlrd` | `>=2.0` | XLS binarios de BBVA, Macro y del FBS |
| `anthropic` | `>=0.90` | `claude-sonnet-5` para sugerencias, `claude-haiku-4-5` para el análisis |
| `psycopg2-binary` | `>=2.9` | Postgres del hub, solo lectura |
| `pymssql` y `pyodbc` | `>=2.3` y `>=5.0`, solo en `fbs-sql` | SQL Server del FBS |

No hay lockfile: las ocho líneas de `requirements.txt` usan `>=` [`requirements.txt`].

No hay base de datos propia. El estado son trece archivos JSON de nombre fijo en `datos/`, más uno por corrida y uno por tablero diario. Esa carpeta no está versionada [`app.py:78-82,1111`; `engine/*`; `.gitignore:1-4`].

## Puesta en marcha

Prerequisitos: Python 3.12 o 3.13 (probado con 3.13). En Windows alcanza con el `.bat`.

```bat
iniciar.bat
```

En cualquier otro sistema:

```bash
pip install -r requirements.txt
python app.py
```

Con Docker:

```bash
docker build -t conciliador .
docker run -p 8765:8765 conciliador
```

Variables de entorno. Ninguna es obligatoria para levantar el servicio; cambia qué funciona.

| Nombre | Para qué | Si falta |
|---|---|---|
| `PORT` | Puerto del servidor | Escucha en 8765, en `127.0.0.1` [`app.py:1801`] |
| `ANTHROPIC_API_KEY` o `ANTHROPIC_AUTH_TOKEN` | Sugerencias y análisis con Claude | La conciliación determinística funciona igual; la IA queda en `sin_credenciales` [`app.py:281-283`] |
| `DATABASE_URL` | Postgres del hub, de donde salen marcas y cuentas | Usa `datos/cuentas_nave.json` o la semilla del código [`engine/cuentas.py:82-105`] |
| `DEMO_MODE` | Ambiente de demostración | Las rutas y la interfaz de demostración no existen [`app.py:51`] |
| `ANALISIS_MAX_DIA` | Tope diario de análisis con IA | Vale 150 [`engine/analisis.py:27`] |
| `FBS_SQL_SERVIDOR`, `FBS_SQL_PUERTO`, `FBS_SQL_BASE`, `FBS_SQL_USUARIO`, `FBS_SQL_CLAVE`, `FBS_SQL_TDS`, `FBS_SQL_DRIVER` | Conexión al FBS, solo en `fbs-sql` | Sin ellas no hay lectura directa del mayor [`76f791e`] |

La credencial de IA también se puede dejar en `datos/anthropic_key.txt`, que el proceso carga al entorno al arrancar [`engine/ai_assist.py:18-33`]. No lo recomendamos: el valor queda en texto plano dentro del volumen.

## Cómo se corre

Abrí `http://localhost:8765`. No hay seed ni usuario de prueba. El modo mensual necesita los dos archivos; el diario, al menos un extracto reconocible.

Para probar la interfaz sin gastar API, entrá con `?simular` en la URL: genera sugerencias marcadas como SIMULACIÓN, emulando la latencia por tandas [`engine/ai_assist.py:116-151`].

Con `DEMO_MODE=true` aparece el kit de extractos descargable, el mayor sembrado se inyecta solo y hay un botón para reiniciar la demostración [`README-DEMO.md`].

## Tests

No hay. Ni `tests/`, ni pytest, ni CI [`find`; `requirements.txt`]. Lo más cercano es el `RESULTADOS_ESPERADOS.md` que genera el kit de demostración, verificado a mano una vez y con la advertencia escrita de que re-correrlo no revalida nada [`scripts/generar_kit_demo.py:217-220`].

Sin cobertura quedan los parsers binarios, el ciclo O→E, la memoria, el arrastre, la nota de débito y el Excel.

## Deploy

Producción en Railway, construida desde el `Dockerfile`, que es el único archivo que define el deploy. Hace falta un Volume montado en `/app/datos`: sin eso, cada redeploy borra reglas, equivalencias, memoria, arrastre y conciliaciones [`README.md:85-86`]. El paso a paso está en `docs/operacion.md`.

## Mapa del repo

- `app.py` — 1.803 líneas: rutas, persistencia, ciclo O→E, arrastre, vetos y el Excel de hasta trece hojas.
- `engine/` — nueve módulos: `matcher.py` (motor y nota de débito), `analisis.py`, `cuentas.py`, `ai_assist.py`, `equivalencias.py`, `reglas.py`, `gastos_conf.py`, `ia_log.py`.
- `parsers/` — `diarios.py` (multibanco y reportes FBS), `santander_pdf.py`, `mayor_xlsx.py`.
- `static/` — `index.html` (mensual) y `diario.html` (diario), sin framework.
- `scripts/` — `generar_kit_demo.py` y, en `fbs-sql`, `migrar_aprendizajes.py`.
- `datos/` — todo el estado, fuera del control de versiones.

Los cinco archivos donde está la lógica: `app.py`, `engine/matcher.py`, `parsers/diarios.py`, `engine/cuentas.py` y `engine/analisis.py`.

## Documentación

- `docs/arquitectura.md`
- `docs/operacion.md`
- `docs/modelo-de-datos.md`
- `docs/decisiones/`
- `CHANGELOG.md`
- `README-DEMO.md`, sobre el ambiente de demostración

No hay `docs/api.md`: las rutas `/api` son el backend de las dos páginas del propio repo y no tienen consumidores externos.
