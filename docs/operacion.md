# Operación del conciliador bancario

Fecha: 2026-09-16

Runbook para quien tenga que mantener el servicio funcionando sin haberlo escrito. Todo comando de acá se probó en un clon limpio el `[completar: fecha de la prueba, la hace el revisor]`.

Lo primero que hay que saber: el servicio no pide credenciales. Cualquiera con la URL opera, borra y exporta [`6803948`; ADR-0006].

## Entornos

| Entorno | URL | Dónde corre | Rama que despliega | Quién tiene acceso |
|---|---|---|---|---|
| Producción | `[completar: URL de producción, lo sabe Axel]` | Railway, desde el `Dockerfile` | `master` (sin confirmar) | Cualquiera con el link |
| Demostración | `[completar: URL del servicio de demo, lo sabe Lauti]` | Railway, servicio aparte | La misma rama que producción, con `DEMO_MODE=true` | Cualquiera con el link |
| On-premise | `http://<máquina>:8766` en la red del cliente | Windows, máquina del grupo | `fbs-sql` | Quien esté en esa red |

La única URL que aparece en el repo está escrita a mano dentro de `migrar_aprendizajes.py` y apunta a un servicio de Railway [`3e29338`]. No confirmamos que sea la que el cliente usa hoy, ni si hay más de un servicio.

`[completar: qué rama corre en producción y si el servicio de Railway es el único, lo sabe Axel]`

## Variables de entorno

Ninguna es obligatoria para que el servicio levante. Sin valores acá: los valores están en el panel de Railway y, para la instalación local, en el entorno de esa máquina.

| Nombre | Para qué | Obligatoria | Dónde se carga | Quién tiene el valor |
|---|---|---|---|---|
| `PORT` | Puerto y bind del servidor | No | Railway la inyecta sola | Railway |
| `ANTHROPIC_API_KEY` o `ANTHROPIC_AUTH_TOKEN` | Sugerencias y análisis con Claude | No | Railway | Rena |
| `DATABASE_URL` | Postgres del hub, marcas y cuentas | No, pero sin ella el catálogo queda viejo | Railway | Lauti |
| `DEMO_MODE` | Activa el ambiente de demostración | Solo en el servicio de demo | Railway | Lauti |
| `ANALISIS_MAX_DIA` | Tope diario de análisis con IA | No, por defecto 150 | Railway | Axel |
| `FBS_SQL_SERVIDOR`, `FBS_SQL_PUERTO`, `FBS_SQL_BASE`, `FBS_SQL_USUARIO`, `FBS_SQL_CLAVE`, `FBS_SQL_TDS`, `FBS_SQL_DRIVER` | Conexión al SQL Server del FBS, solo en `fbs-sql` | Sí, en la instalación local | Entorno de la máquina Windows | Axel |

Hay un camino alternativo para la credencial de IA: un archivo `datos/anthropic_key.txt` que el proceso carga al entorno al arrancar [`engine/ai_assist.py:18-33`]. Si aparece en un servicio, conviene mover el valor a la variable y borrar el archivo.

En modo hub de la rama `fbs-sql`, la credencial del SQL Server no viaja por variable: se lee de la fila de `fbs_conexiones` en el Postgres del hub, en texto [`bbc35bc`]. Nunca se devuelve por API [`8cb00f3`].

## Deploy

1. En Railway, el servicio apunta al repositorio de GitHub y detecta el `Dockerfile` solo. No hay `railway.json`, `Procfile` ni CI: el `Dockerfile` es lo único que define el build [`find`].
2. Cargar `ANTHROPIC_API_KEY` y `DATABASE_URL` en Variables.
3. Settings → Networking → Generate Domain.
4. Montar un Volume en `/app/datos`. Este paso no es opcional aunque el README viejo lo marque así: sin volumen, cada redeploy borra reglas, equivalencias, memoria, arrastre y todas las conciliaciones guardadas [`README.md:85-86`; `app.py:356-358`].

**Cómo verificar que salió bien.** Abrir `GET /api/diagnostico`. Devuelve si la credencial de IA llegó al proceso (presencia, longitud, prefijo y espacios al borde, nunca el valor), si `datos/` está montado como volumen, el gasto de IA del día y el estado del modo hub del FBS [`app.py:339-366`; `bc3d9b9`]. Después, abrir `/` y `/diario` y confirmar que la lista de cuentas trae las del hub.

**Rollback.** Redeploy de la build anterior desde Railway. Nunca se probó. El riesgo real no es el código sino el estado: si el volumen no estaba montado, volver atrás no recupera lo que se perdió.

En la instalación on-premise el deploy es un `git pull`. El `.bat` del puerto 8766 recarga el servidor solo cuando cambia un archivo `.py` [`f8593cf`]. Ese mismo `.bat` mata con `taskkill /f` cualquier proceso que esté escuchando en el 8765, así que no conviene correr las dos instalaciones a la vez en la misma máquina [`b559d13`].

## Base de datos

No hay migraciones porque no hay base propia: el estado son trece archivos JSON de nombre fijo en `datos/`, más uno por corrida y uno por tablero diario [ADR-0007]. El Postgres del hub se lee y no se le escribe, así que el conciliador nunca corre migraciones sobre él [`engine/cuentas.py:111-114`].

**Backups.** No encontramos ninguna rutina de respaldo en el sistema ni en el despliegue: ni job, ni script, ni paso del deploy. Si Railway hace copias del volumen, no está registrado en ningún lado. `[completar: si el volumen de Railway tiene backups, lo sabe Axel Wdoviak]` Lo más parecido que hay en el propio sistema es `GET /api/migracion/exportar`, que baja todos los archivos de `datos/` en un bundle, y su contraparte de importación, que solo entra en un servicio sin conciliaciones previas salvo que se fuerce con `forzar=true` [`app.py:449-488`].

**Restore.** Nunca probado. El procedimiento sería levantar el servicio con el volumen montado y hacer la importación del bundle antes de la primera corrida.

Recomendamos programar una exportación periódica del bundle a un lugar fuera de Railway. El costo de no hacerlo: un volumen perdido se lleva todo el aprendizaje acumulado desde julio.

## Tareas programadas

Ninguna. Lo único que corre solo es la regeneración del kit de demostración al arrancar el proceso, y solo cuando `DEMO_MODE` está activa [`app.py:1774-1781`].

## Monitoreo y logs

Los logs son `print()` a stdout, visibles en el panel de Railway. No hay configuración de logging, ni niveles, ni archivo.

No hay healthcheck dedicado ni `healthcheckPath` configurado. `GET /api/diagnostico` cumple esa función y no devuelve valores de secretos [`app.py:339-366`].

Alertas: nadie las recibe. No hay monitoreo externo, ni aviso por caída, ni aviso cuando el catálogo de cuentas cae al respaldo local (eso solo se imprime en el log) [`engine/cuentas.py:104,127`].

## Incidentes conocidos

**Cruces contra asientos ajenos.** Mismo importe, asiento equivocado. El ciclo entre la O y la E confirmaba contra cualquier asiento que coincidiera en importe "y los consumía para siempre vía la memoria". Se corrigió el 2026-09-15 exigiendo referencia, importe y lado exactos [`f6a9a8c`].

**Proveedores tomados por gastos.** El término `iva ` matcheaba "cooperatIVA de seguros" y `com ` matcheaba "teleCOM". Pasó con pagos reales. Se corrigió agregando límite de palabra al detector [`acde316`; `engine/matcher.py:20-22`].

**Comprobantes alfanuméricos.** Rompían el detector de gastos. Un comprobante como `00014Q` dejaba una letra suelta que no matcheaba nada; los términos pasaron a descartar el token entero cuando tiene dígitos [`e4b7e4d`].

**Material de demostración versionado.** Volvió a entrar al repositorio por la prueba de arranque y hubo que sacarlo de nuevo [`8b3c5c1`].

Al 2026-09-16 quedan dos tickets abiertos sobre esto, los dos de Axel y los dos In Progress: `OR-26`, un bug de un gasto que no se reconoce, y `OR-27`, una mejora en la memoria del modo diario [`[jira-OR.md]`].

## Procedimientos frecuentes

**Agregar una cuenta bancaria.** Se carga en el hub, no acá: el conciliador la lee del Postgres y la muestra en menos de sesenta segundos, que es lo que dura la caché [`engine/cuentas.py:26`]. Si el hub no está disponible, la cuenta no aparece hasta que vuelva.

**Vincular una cuenta con el FBS.** La primera vez que se sube un reporte del FBS de una cuenta nueva, el sistema pide asignarle el código de cuenta contable. Queda aprendido y no hay que repetirlo [`app.py:963-978`].

**Enseñarle un concepto nuevo.** Si el banco y el mayor llaman distinto a lo mismo, se carga el par en la pantalla de equivalencias. Si un concepto se está clasificando mal como gasto, desde el modal de Transferir se marca "este concepto nunca es gasto bancario" [`f7a0954`].

**Deshacer un cruce equivocado.** `POST /api/anular/{job_id}` lo anula y lo veta para siempre; la conciliación manual se deshace con `DELETE /api/manual/{job_id}/{match_id}`, que además olvida la regla que había derivado [`app.py:1499-1553`].

**Rotar la credencial de IA.** Cambiar la variable en Railway, redeploy, y verificar en `/api/diagnostico` que el proceso la tomó. Si existía un `datos/anthropic_key.txt`, borrarlo: tiene prioridad sobre lo que se cargue.

**Copiar el aprendizaje.** A la instalación local: `migrar_aprendizajes.py` trae equivalencias, gastos, reglas y mapeos desde Railway sin tocar las corridas locales [`3e29338`].
