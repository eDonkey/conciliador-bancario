# Auditoría de seguridad del conciliador (7/10/2026)

Rama: `auditoria-seguridad`, creada desde `fbs-sql`.

Por qué `fbs-sql` y no `master`: `fbs-sql` contiene a `master` (es descendiente
directo, +47 commits) y es la única que tiene el código que el hallazgo cita: la
conexión directa al FBS (`engine/fbs_sql.py`), el modo diario multibanco, la
telemetría y la demo. Arreglar sobre `master` dejaría afuera justamente el código
vulnerable. El arreglo es solo de código (no toca configuración de ningún cliente),
así que se puede fusionar a las ramas de cada instalación después.

## Qué se arregló

### CRÍTICO: la app no tenía login
- Autenticación obligatoria para TODA ruta (HTML, API y estáticos) salvo `/login` y
  `/health`. Contraseña en `APP_PASSWORD` y cookie de sesión firmada
  (HMAC-SHA256, `HttpOnly`, `SameSite=Lax`, `Secure` si el pedido llegó por https).
  Mismo criterio que compensador (`APP_PASSWORD`), pero con pantalla de login y
  sesión con vencimiento en vez de Basic. Para el acceso por el hub ver
  "Identidad firmada del hub" más abajo.
- Sin `APP_PASSWORD` la app NO se abre: responde 503 a todo acceso remoto. Solo
  atiende a quien llega por loopback sin pasar por un proxy (si el pedido trae
  `X-Forwarded-*`, `CF-Connecting-IP`, `X-Orbit-*`, etc., no cuenta como local, así
  que el hub y el túnel quedan rechazados sin contraseña).
- Sesión: 12 h sin uso (`SESION_HORAS`), renovación deslizante y tope absoluto de
  7 días (`SESION_MAX_DIAS`). Cambiar `APP_PASSWORD` (o `SESSION_SECRET`) cierra
  todas las sesiones. Botón "Salir" en las dos pantallas y redirección al login
  cuando una llamada devuelve 401.
- Login con límite de intentos: 10 fallos por IP cada 15 min y 60 globales (para que
  cambiar de IP no dé intentos infinitos). Respuesta 429 con `Retry-After`.
- CSRF: en métodos que modifican se rechaza un `Origin` que no sea el del servicio
  (`ORIGENES_PERMITIDOS` para agregar otros), además de `SameSite=Lax`.
- Rutas con `\`, `..` o caracteres de control se rechazan (los ids de la URL terminan
  siendo nombres de archivo en `datos/`; en Windows `%5C` permitía salir de la carpeta).
- `uvicorn` arranca con `proxy_headers=False` (`app.py` y `Dockerfile`): la IP y
  "es local" las decide `engine/seguridad.py`, no un `X-Forwarded-For` creído a ciegas.

### CRÍTICO: apuntar la conexión FBS a otro servidor
- Cambiar servidor/puerto/base/usuario/clave por `POST /api/fbs-sql` ahora exige
  estar en la propia máquina o tener `FBS_SQL_EDITABLE=1`; si no, 403. La query sigue
  siendo editable (con validación). Si `FBS_SQL_SERVIDOR` está en el entorno, el
  destino queda fijo.
- Cambiar el destino sin mandar clave nueva borra la clave guardada (no se reenvía
  la clave vieja a otro servidor).
- `validar_destino()` se aplica al guardar y en cada consulta (también a las
  conexiones que vienen del hub): el host debe ser nombre/IP bien formado, con
  puerto válido (se excluyen 22, 25, 80, 443, 3389, etc.) y resolver a una dirección
  privada (RFC 1918 o 100.64/10). Públicas o loopback solo si están en
  `FBS_SQL_HOSTS_PERMITIDOS`. Link-local (169.254.x, metadatos de nube), multicast
  y 0.0.0.0 nunca.
- `datos/fbs_sql.json` (con la clave) se escribe de forma atómica con permisos 0600
  y ya no sale por `/api/migracion/exportar` ni se acepta por `/importar` (antes
  exportaba la clave del FBS aunque el comentario decía lo contrario, y el importar
  permitía redirigir el destino).
- `/api/diagnostico` ya no lista nombres de variables de entorno ni el largo de la
  clave de Anthropic.

### Solo lectura del FBS (se verificó el chequeo)
El validador anterior tenía un bypass real: borraba comentarios `--` ANTES de sacar
los literales, así que `SELECT '--' ; DROP TABLE x` se veía como `SELECT` y SQL
Server ejecutaba el `DROP`. Ahora un lector único entiende literales, identificadores
`[..]`/`".."` y comentarios `/* */` anidados como SQL Server, falla si algo queda sin
cerrar, y la lista de palabras prohibidas creció (`declare`, `set`, `waitfor`,
`begin`, `while`, `go`, `fn_*`, `openxml`, `commit`, etc.). Más una tercera capa:
antes de la primera consulta de cada conexión se verifica que el login NO sea
`sysadmin`, `db_owner`, `db_datawriter` ni `db_ddladmin`; si lo es, se rechaza
(`FBS_SQL_PERMITIR_LOGIN_ESCRITURA=1` lo permite como excepción consciente). El
rollback explícito y `ApplicationIntent=ReadOnly` (ODBC) se mantienen/agregan.

### Números de cuenta reales en el código
La semilla de 30 cuentas salió de `engine/cuentas.py`. Ahora se lee de
`CUENTAS_SEMILLA_RUTA` o de `config/cuentas_semilla.json` (en `.gitignore`); el
formato está en `config/cuentas_semilla.example.json` (commiteado, con números
ficticios). Sin archivo la semilla queda vacía y la app depende del hub (la fuente
real es el Postgres del hub). En esta máquina dejé `config/cuentas_semilla.json` con
los valores originales para que el desarrollo siga andando; no se commitea.

### IP del cliente
`engine/seguridad.ip_cliente()`: en Railway se toma el ÚLTIMO elemento de
`X-Forwarded-For` (el que agrega el borde de Railway); detrás del túnel de Cloudflare
se usa `CF-Connecting-IP` solo si el socket es loopback; si no, la IP del socket.
Se autodetecta Railway por sus variables; `PROXY_MODE=railway|cloudflare|none` lo
fuerza. Se usa para el límite de intentos de login.

### Identidad firmada del hub (hallazgo 11)
El hub firma los encabezados `X-Orbit-*` con el contrato de `hub/docs/FIRMA_ORBIT.md`
(`X-Orbit-Timestamp` + `X-Orbit-Firma` `v1=`, HMAC-SHA256 sobre método, ruta con query y
todos los `X-Orbit-*`; el mismo módulo que gerencia, ventas, adm-ventas, adm-planes y
parte-diario). `engine/seguridad.identidad_hub()` lo verifica y `engine/acceso.py` lo usa
antes del chequeo de `APP_PASSWORD`:

- Pedido con `X-Orbit: 1`, firma válida (clave configurada, método y ruta de ese pedido,
  60 s) y un `X-Orbit-Usuario`: queda autenticado sin pedir contraseña, aunque `APP_PASSWORD`
  no esté configurada (el hub antes recibía 503). Sigue valiendo el control de `Origin` en
  los métodos que modifican. El usuario y los permisos quedan en `request.state.hub`.
- Sin clave de firma, o con firma ausente/mala/vencida/de otra ruta, o sin usuario: esas
  cabeceras no valen nada y solo funciona el login propio (contraseña + cookie), igual que
  antes. Acceso directo (puerto de la app, sin hub) no cambia.
- Clave: `HUB_FIRMA_CLAVE` (alias `ORBIT_FIRMA_CLAVE`) o, si no está, la derivada de
  `HUB_CLAVE` (la misma del hub); `HUB_CLAVE_ANTERIOR` se acepta mientras se rota.
- Requiere que el hub envíe la identidad a esta herramienta (rama `auditoria-seguridad`
  del hub: las herramientas sin conexión ahora reciben `X-Orbit` firmado).

### Presupuesto diario de tokens de IA
`engine/presupuesto_ia.py`: tope diario de tokens (entrada+salida, contados con el
uso real de cada respuesta) en `IA_TOKENS_DIA` (por defecto 2.000.000; 0 apaga la IA).
Aviso en el log al 80 % (`IA_AVISO_PORCENTAJE`, una vez por día) y estado en
`/api/diagnostico` (`ia_presupuesto`). Al pasarlo: 429 en `/api/conciliar` (con
`usar_ia=si`) y en `/api/analizar`; durante un job la IA se corta y la conciliación
sigue sin IA. El modelo se define solo en el servidor (`IA_MODELO`,
`IA_MODELO_ANALISIS`); ningún endpoint lo recibe del navegador (hay test). El cupo
anterior de 150 análisis/día (`ANALISIS_MAX_DIA`) sigue vigente.

### Backup de `datos/`
`scripts/backup_datos.py` y `scripts/backup_datos.bat`: zip con fecha de toda la
carpeta `datos/`, retención de 30 días, sin credenciales por defecto
(`--con-secretos` las incluye). Para sumarlo al backup nocturno del servidor Nave
(tarea "Backup hub", 03:00, `C:\backups`), agregar al `backup_hub.bat` la línea:
`call C:\ruta\al\conciliador\scripts\backup_datos.bat C:\backups`
y sincronizar `C:\backups` fuera de la máquina (pendiente del hallazgo 1).

### Tests
`tests/` (81 tests, `python -m pytest tests`): login, sesión vencida/adulterada, límite de
intentos con `X-Forwarded-For` falso, rechazo remoto sin clave, CSRF, IP del cliente,
validador de solo lectura (incluido el bypass del `--`), destinos FBS, chequeo de
privilegios, presupuesto con aviso 80 % y 429, modelo solo por servidor, semilla de
cuentas e identidad firmada del hub (`tests/test_firma_hub.py`: vector fijo del contrato, firma
mala/vencida/de otra ruta, sin clave, rotación, CSRF, login propio intacto). El repo no tenía tests antes.

## Variables a configurar ANTES de desplegar

| Variable | Obligatoria | Para qué |
|---|---|---|
| `APP_PASSWORD` | Sí, en todo despliegue | Contraseña de la app. Sin ella todo acceso remoto da 503 (incluida la demo de Railway, el hub y el túnel). |
| `SESSION_SECRET` | Recomendada | Clave de firma de la sesión (si falta se deriva de `APP_PASSWORD`). |
| `FBS_SQL_PERMITIR_LOGIN_ESCRITURA=1` | Solo si hace falta | Si hoy el login del FBS es admin o db_owner la app va a rechazar las consultas hasta crear un login `db_datareader`. Probar con `POST /api/fbs-sql/probar` antes de dar por bueno el despliegue. |
| `FBS_SQL_HOSTS_PERMITIDOS` | Si el FBS no está en una IP privada | Nombres/IPs/CIDR permitidos (p. ej. el contenedor de Verona en 127.0.0.1 o un FBS por IP pública). |
| `FBS_SQL_EDITABLE=1` | Solo si se edita la conexión manual desde la web | Por defecto solo se edita desde la propia máquina. En modo hub no hace falta. |
| `IA_TOKENS_DIA`, `IA_AVISO_PORCENTAJE` | Opcionales | Tope (def. 2.000.000) y umbral de aviso (def. 80). Falta decidir el número, ver pendientes. |
| `IA_MODELO`, `IA_MODELO_ANALISIS` | Opcionales | Modelos (def. `claude-sonnet-5` / `claude-haiku-4-5`). |
| `HUB_FIRMA_CLAVE` (alias `ORBIT_FIRMA_CLAVE`) o `HUB_CLAVE` / `HUB_CLAVE_ANTERIOR` | Para entrar por el hub sin contraseña | La misma clave del hub. Si el hub ya tiene `HUB_CLAVE`, alcanza con poner la misma `HUB_CLAVE` acá (se deriva la clave de firma). Sin ninguna, el hub no puede autenticarse y rige `APP_PASSWORD`. `ORBIT_FIRMA_TOLERANCIA` (60 s) opcional. |
| `PROXY_MODE` | Opcional | `railway`, `cloudflare` o `none` si la autodetección no alcanza. |
| `SESION_HORAS`, `SESION_MAX_DIAS`, `ORIGENES_PERMITIDOS` | Opcionales | Vida de la sesión y orígenes extra aceptados en POST. |
| `CUENTAS_SEMILLA_RUTA` | Opcional | Ruta del JSON de cuentas de respaldo (si no, `config/cuentas_semilla.json`). |

Además: copiar `config/cuentas_semilla.json` al servidor si se quiere el respaldo
local de cuentas, y servir siempre por https (la cookie se marca `Secure` solo si ve
`X-Forwarded-Proto: https`). Los usuarios tendrán que ingresar una vez.

## Qué queda pendiente y por qué

1. **Sacar la URL de Railway de internet / ponerlo detrás del hub.** Es decisión de
   despliegue, no de código. El login cierra el hueco, pero mientras la URL siga
   viva sigue expuesta a fuerza bruta (limitada) y a escaneo.
2. **Cambiar la clave del FBS si alguna vez se cargó en Railway** (modo manual /
   `FBS_SQL_CLAVE`): solo el equipo puede saberlo.
3. **Los números de cuenta siguen en el historial de git** (y en `origin`). Sacarlos
   exige reescribir historia (`git filter-repo`) y coordinar un force-push: no lo
   hice (nada de push). Mientras tanto no están en el árbol de trabajo.
4. **Backups**: el script existe, pero falta (a) agregarlo al backup nocturno real,
   (b) copia fuera del servidor/Railway, (c) prueba de restauración trimestral.
   Si `datos/` está en un volumen de Railway hay que correrlo ahí o bajar el volumen.
5. **Cuánto gastar por día**: 2.000.000 de tokens es un valor provisional. El aviso
   del 80 % hoy solo queda en el log y en `/api/diagnostico`; falta definir quién lo
   recibe y conectarlo a una alerta (New Relic).
6. **Identidad del hub**: hecho el lado de la app (ver "Identidad firmada del hub"). Falta
   desplegar el hub con la firma y la clave en las dos puntas. La contraseña única sigue
   siendo la deuda del hallazgo 3 para el acceso directo; por el hub ya llega el usuario
   (queda en `request.state.hub`, todavía no se registra "quién hizo qué" en `datos/`).
7. **Datos del lado del servidor**: sin plazo de conservación (hallazgo 2, `datos/`
   crece para siempre) y sin migraciones/versionado de los JSON. Falta la política.
8. **Qué rama corre cada instalación**: cada instalación debería tener su rama con la
   configuración fuera del repo; esta rama es solo código y hay que fusionarla a las
   ramas de cliente a pedido.
9. **Telemetría**: `engine/telemetria.py` manda a New Relic datos que la auditoría
   marca como sensibles (hallazgo 8); es un módulo compartido que se corrige en
   `monitor/`, no acá.
10. **Cuentas del hub**: las contraseñas FBS del hub están en texto plano en su base
    (hallazgo 12); el conciliador las lee de ahí. Se arregla en el hub
    (`HUB_FBS_CIFRADO`), y este código tendrá que leerlas descifradas cuando se active.
11. `/api/migracion/importar` sigue pudiendo sobrescribir datos (con `forzar`), ahora
    solo para usuarios autenticados y sin tocar la conexión FBS.
