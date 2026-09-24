# -*- coding: utf-8 -*-
"""Telemetría del servicio (monitoreo): errores, trazas, métricas y logs.

Manda todo por OpenTelemetry a New Relic (o a cualquier backend OTLP/HTTP)
con el GRUPO y el SERVICIO en cada dato, así un mismo tablero y las mismas
alertas sirven para todos los grupos donde se instalen las apps.

FUENTE ÚNICA: monitor/python/telemetria.py. Cada app tiene una copia idéntica
en engine/telemetria.py que se actualiza con `python monitor/sincronizar.py`.
No editar la copia de una app: se pisa en la próxima sincronización.

Uso (app.py, apenas se crea la app):

    from engine import telemetria
    app = FastAPI(...)
    telemetria.iniciar(app, "conciliador", puerto=8765)

Errores que la app maneja (no llegan como 500) pero que hay que ver igual:

    except Exception as exc:
        telemetria.registrar_error(exc, origen="job", job=job_id)

Tramos propios, para medir lo que importa (consultas al FBS, jobs, IA):

    with telemetria.tramo("FBS consulta", tipo="cliente", marca=marca) as t:
        filas = ...
        t.set_attribute("fbs.filas", len(filas))

Variables (del entorno o del .env al lado de app.py, sin pisar el entorno):

    NEW_RELIC_LICENSE_KEY   license key de ingesta: activa el envío a New Relic
    NEW_RELIC_REGION        us (por defecto) | eu
    OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_HEADERS
                            otro backend OTLP/HTTP (Grafana Cloud, SigNoz...)
    MONITOR_GRUPO           grupo o cliente dueño de la instalación (nave-motors)
    MONITOR_ENTORNO         produccion | demo | desarrollo
    MONITOR_DESPLIEGUE      on-premise | railway (se detecta solo)
    MONITOR_MUESTREO        fracción de trazas que se guardan (1 = todas)
    MONITOR_NIVEL_LOGS      nivel mínimo de logs que se envían (INFO)
    MONITOR_INTERVALO       segundos entre envíos de métricas (60)
    MONITOR_ACTIVO=0        apaga la telemetría aunque haya destino

Nunca rompe la app: sin destino, sin los paquetes instalados o ante cualquier
falla propia queda en modo nulo y la app sigue exactamente igual.
"""
import atexit
import importlib
import importlib.util
import logging
import os
import re
import shutil
import socket
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from urllib.parse import unquote, urlparse

VERSION_MODULO = "2026.09.24"

# carpeta de la app (este archivo vive en <app>/engine/)
_RAIZ_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Lo que no vale la pena trazar: estáticos, el chequeo del vigía del hub y el
# ícono. Son regex separadas por coma (no usar comas dentro de una regex).
_EXCLUIR_SIEMPRE = (
    r"/static/",
    r"\.(?:js|css|map|png|jpe?g|gif|svg|ico|woff2?|ttf)(?:\?|$)",
    r"favicon",
    r"/__monitor",
)

_estado = {"activo": False, "motivo": "sin iniciar"}
_tracer = None
_contador_errores = None
_proveedores = []
_inicio = time.time()
_log = logging.getLogger("monitor")


class _TramoNulo:
    """Lo que devuelve tramo() sin telemetría: acepta todo y no hace nada."""

    def set_attribute(self, *a, **k):
        pass

    def set_attributes(self, *a, **k):
        pass

    def add_event(self, *a, **k):
        pass

    def record_exception(self, *a, **k):
        pass

    def set_status(self, *a, **k):
        pass

    def is_recording(self):
        return False


_TRAMO_NULO = _TramoNulo()


# ---------------------------------------------------------------- utilidades
def _verdadero(valor) -> bool:
    return str(valor or "").strip().lower() in ("1", "true", "on", "si", "sí", "yes")


def _slug(texto) -> str:
    t = unicodedata.normalize("NFD", str(texto or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")


def _recortar(texto, largo=1000) -> str:
    texto = str(texto)
    return texto if len(texto) <= largo else texto[: largo - 1] + "…"


def _limpiar(atributos: dict) -> dict:
    """Atributos aceptados por OpenTelemetry: escalares o listas de escalares."""
    limpios = {}
    for clave, valor in (atributos or {}).items():
        if valor is None:
            continue
        if isinstance(valor, (bool, int, float)):
            limpios[str(clave)] = valor
        elif isinstance(valor, (list, tuple, set)):
            limpios[str(clave)] = [_recortar(v, 300) for v in list(valor)[:50]]
        else:
            limpios[str(clave)] = _recortar(valor)
    return limpios


def _cargar_env_local():
    """Suma las variables de monitoreo del .env de la app, sin pisar el entorno.
    Solo MONITOR_*, NEW_RELIC_* y OTEL_*: el resto lo carga cada app a su modo."""
    try:
        with open(os.path.join(_RAIZ_APP, ".env"), encoding="utf-8-sig") as f:
            lineas = f.read().splitlines()
    except OSError:
        return
    for linea in lineas:
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", linea)
        if not m or not m.group(1).startswith(("MONITOR_", "NEW_RELIC_", "OTEL_")):
            continue
        valor = m.group(2)
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        os.environ.setdefault(m.group(1), valor)


def _version_git():
    """(rama, commit corto) de lo que está corriendo, leyendo .git sin llamar a
    git. En Railway vienen en variables del deploy."""
    rama = os.environ.get("RAILWAY_GIT_BRANCH", "")
    commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7]
    if commit:
        return rama, commit
    try:
        git = os.path.join(_RAIZ_APP, ".git")
        if os.path.isfile(git):                      # worktree: "gitdir: <ruta>"
            with open(git, encoding="utf-8") as f:
                git = os.path.normpath(os.path.join(_RAIZ_APP, f.read().split(":", 1)[1].strip()))
        comun = git
        if os.path.isfile(os.path.join(git, "commondir")):
            with open(os.path.join(git, "commondir"), encoding="utf-8") as f:
                comun = os.path.normpath(os.path.join(git, f.read().strip()))
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
        if not head.startswith("ref:"):
            return "", head[:7]                      # HEAD suelto
        ref = head[4:].strip()
        rama = ref.split("refs/heads/", 1)[-1]
        ruta_ref = os.path.join(comun, *ref.split("/"))
        if os.path.isfile(ruta_ref):
            with open(ruta_ref, encoding="utf-8") as f:
                commit = f.read().strip()[:7]
        elif os.path.isfile(os.path.join(comun, "packed-refs")):
            with open(os.path.join(comun, "packed-refs"), encoding="utf-8") as f:
                for linea in f:
                    if linea.rstrip().endswith(" " + ref):
                        commit = linea.split()[0][:7]
                        break
    except (OSError, IndexError):
        pass
    return rama, commit


def _puerto(por_defecto) -> str:
    if os.environ.get("PORT"):
        return os.environ["PORT"].strip()
    args = sys.argv
    for i, arg in enumerate(args):
        if arg == "--port" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--port="):
            return arg.split("=", 1)[1]
    return str(por_defecto or "")


def _entorno() -> str:
    valor = _slug(os.environ.get("MONITOR_ENTORNO", ""))
    if valor:
        return valor
    # cada app tiene su bandera de demo (DEMO_MODE, PANEL_DEMO, COMPENSADOR_DEMO_MODE...)
    if any(_verdadero(v) for k, v in os.environ.items()
           if k == "DEMO_MODE" or k.endswith(("_DEMO", "_DEMO_MODE"))):
        return "demo"
    return "produccion"


def _despliegue() -> str:
    valor = _slug(os.environ.get("MONITOR_DESPLIEGUE", ""))
    if valor:
        return valor
    if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"):
        return "railway"
    return "on-premise" if os.name == "nt" else "servidor"


def _cabeceras(texto: str) -> dict:
    """OTEL_EXPORTER_OTLP_HEADERS: "clave=valor,clave2=valor2" (url-encoded)."""
    cab = {}
    for parte in str(texto or "").split(","):
        if "=" in parte:
            clave, valor = parte.split("=", 1)
            if clave.strip():
                cab[unquote(clave.strip())] = unquote(valor.strip())
    return cab


def _destino():
    """(url base, cabeceras, nombre) del backend OTLP/HTTP; None si no hay."""
    clave = (os.environ.get("NEW_RELIC_LICENSE_KEY") or "").strip().strip("\"'")
    base = (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    if base:
        cabeceras = _cabeceras(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""))
        es_nr = (urlparse(base).hostname or "").endswith("nr-data.net")
        # la license key solo viaja a New Relic, nunca a otro backend
        if clave and es_nr and not any(k.lower() == "api-key" for k in cabeceras):
            cabeceras["api-key"] = clave
        return base.rstrip("/"), cabeceras, "new-relic" if es_nr else "otlp"
    if clave:
        eu = os.environ.get("NEW_RELIC_REGION", "").strip().lower() == "eu"
        base = "https://otlp.eu01.nr-data.net" if eu else "https://otlp.nr-data.net"
        return base, {"api-key": clave}, "new-relic"
    return None


def _severidad(nivel: int):
    from opentelemetry._logs import SeverityNumber
    if nivel >= logging.CRITICAL:
        return SeverityNumber.FATAL, "FATAL"
    if nivel >= logging.ERROR:
        return SeverityNumber.ERROR, "ERROR"
    if nivel >= logging.WARNING:
        return SeverityNumber.WARN, "WARN"
    if nivel >= logging.INFO:
        return SeverityNumber.INFO, "INFO"
    return SeverityNumber.DEBUG, "DEBUG"


class _ManejadorLogs(logging.Handler):
    """Manda los logs de Python como logs OTLP, con la traza y la excepción."""

    def __init__(self, proveedor, nivel):
        super().__init__(level=nivel)
        self._proveedor = proveedor
        self._local = threading.local()

    def emit(self, record):
        # los logs del propio envío (exportador, urllib3) no se re-envían: si
        # el backend está caído sería un bucle
        if record.name.startswith(("opentelemetry", "urllib3")) or getattr(self._local, "dentro", False):
            return
        self._local.dentro = True
        try:
            try:
                cuerpo = record.getMessage()
            except Exception:  # noqa: BLE001 — un log mal formado igual sale
                cuerpo = str(record.msg)
            atributos = {
                "logger.name": record.name,
                "code.function.name": record.funcName,
                "code.file.path": record.pathname,
                "code.line.number": record.lineno,
                "thread.name": record.threadName,
            }
            extra = getattr(record, "monitor", None)
            if isinstance(extra, dict):
                atributos.update(extra)
            numero, texto = _severidad(record.levelno)
            excepcion = record.exc_info[1] if record.exc_info else None
            self._proveedor.get_logger(record.name).emit(
                timestamp=int(record.created * 1e9),
                severity_number=numero,
                severity_text=texto,
                body=_recortar(cuerpo, 8000),
                attributes=_limpiar(atributos),
                exception=excepcion,
            )
        except Exception:  # noqa: BLE001 — el monitoreo nunca rompe la app
            pass
        finally:
            self._local.dentro = False


# ------------------------------------------------------------------ arranque
def iniciar(app=None, servicio: str = "", *, puerto=None, excluir=(), carpeta_datos=None) -> bool:
    """Configura la telemetría del proceso. Devuelve True si quedó activa.

    app            la app FastAPI (None para procesos sin HTTP)
    servicio       nombre corto y fijo del servicio: conciliador, ventas...
    puerto         puerto por defecto de la app (si no viene en PORT o --port)
    excluir        regex extra de URLs que no se trazan (polling, p. ej.)
    carpeta_datos  carpeta cuyo disco se vigila (por defecto la de la app)
    """
    if _estado["activo"]:
        return True
    try:
        _cargar_env_local()
        servicio = _slug(servicio) or "app"
        grupo = _slug(os.environ.get("MONITOR_GRUPO", "")) or "sin-grupo"
        entorno = _entorno()
        despliegue = _despliegue()
        rama, commit = _version_git()
        version = f"{rama}@{commit}" if rama and commit else (commit or "sin-version")
        host = socket.gethostname()
        instancia = f"{host}:{_puerto(puerto) or 'pid' + str(os.getpid())}"
        _estado.update({
            "servicio": servicio, "grupo": grupo, "nombre": f"{grupo}/{servicio}",
            "entorno": entorno, "despliegue": despliegue, "version": version,
            "instancia": instancia,
        })

        if not _verdadero(os.environ.get("MONITOR_ACTIVO", "1")):
            _estado["motivo"] = "apagada con MONITOR_ACTIVO=0"
            return False
        destino = _destino()
        if not destino:
            _estado["motivo"] = "sin destino: falta NEW_RELIC_LICENSE_KEY (u OTEL_EXPORTER_OTLP_ENDPOINT)"
            return False
        base, cabeceras, backend = destino
        _estado["destino"] = f"{backend} ({urlparse(base).hostname})"

        # convenciones HTTP estables de OpenTelemetry (las que usan los tableros)
        os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
        try:
            _configurar(app, base, cabeceras, excluir, carpeta_datos, {
                "service.name": f"{grupo}/{servicio}",
                "service.namespace": grupo,
                "service.version": version,
                "service.instance.id": instancia,
                "deployment.environment": entorno,
                "deployment.environment.name": entorno,
                "host.name": host,
                # los atributos "tags.*" quedan como tags de la entidad en New
                # Relic: filtran tableros, alertas y workloads por grupo
                "tags.grupo": grupo,
                "tags.servicio": servicio,
                "tags.entorno": entorno,
                "tags.despliegue": despliegue,
                "tags.rama": rama or "-",
            })
        except ImportError as exc:
            _estado["motivo"] = f"faltan paquetes de OpenTelemetry ({exc.name}): pip install -r requirements.txt"
            print(f"[monitor] telemetría desactivada: {_estado['motivo']}", file=sys.stderr)
            return False

        _estado.update({"activo": True, "motivo": ""})
        evento(f"Servicio iniciado: {grupo}/{servicio} {version} en {instancia} ({despliegue}, {entorno})",
               evento="inicio")
        return True
    except Exception as exc:  # noqa: BLE001 — el monitoreo nunca rompe la app
        _estado.update({"activo": False, "motivo": f"error al iniciar: {exc!r}"})
        print(f"[monitor] telemetría desactivada: {exc!r}", file=sys.stderr)
        return False


def _configurar(app, base, cabeceras, excluir, carpeta_datos, atributos):
    global _tracer, _contador_errores
    from opentelemetry import metrics, trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import (Counter, Histogram, MeterProvider, ObservableCounter,
                                           ObservableGauge, ObservableUpDownCounter, UpDownCounter)
    from opentelemetry.sdk.metrics.export import AggregationTemporality, PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    if app is not None:
        # se importa acá para que, si falta, no quede nada configurado a medias
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: F401

    recurso = Resource.create(atributos)
    comun = {"headers": cabeceras, "timeout": 5, "compression": Compression.Gzip}

    try:
        muestreo = min(1.0, max(0.0, float(os.environ.get("MONITOR_MUESTREO") or 1)))
    except ValueError:
        muestreo = 1.0
    trazas = TracerProvider(resource=recurso, sampler=ParentBased(TraceIdRatioBased(muestreo)))
    trazas.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=base + "/v1/traces", **comun)))
    trace.set_tracer_provider(trazas)
    _tracer = trace.get_tracer("monitor")

    # New Relic pide temporalidad delta para contadores e histogramas
    delta, acumulada = AggregationTemporality.DELTA, AggregationTemporality.CUMULATIVE
    try:
        intervalo = max(5.0, float(os.environ.get("MONITOR_INTERVALO") or 60))
    except ValueError:
        intervalo = 60.0
    lector = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=base + "/v1/metrics", preferred_temporality={
            Counter: delta, Histogram: delta, ObservableCounter: delta,
            UpDownCounter: acumulada, ObservableUpDownCounter: acumulada, ObservableGauge: acumulada,
        }, **comun),
        export_interval_millis=intervalo * 1000,
    )
    medidas = MeterProvider(resource=recurso, metric_readers=[lector])
    metrics.set_meter_provider(medidas)

    logs = LoggerProvider(resource=recurso)
    logs.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=base + "/v1/logs", **comun)))
    set_logger_provider(logs)

    _proveedores.extend([medidas, logs, trazas])
    atexit.register(_cerrar)

    _conectar_logging(logs)
    _metricas(metrics.get_meter("monitor"), carpeta_datos or _RAIZ_APP)
    _contador_errores = metrics.get_meter("monitor").create_counter(
        "monitor.errores", unit="{error}", description="Errores registrados por el servicio")
    _instrumentar(app, excluir, urlparse(base).hostname or "")
    _enganchar_excepciones()


class _Espaciado(logging.Filter):
    """Los avisos del propio envío (backend caído, sin internet, key inválida)
    salen por consola como mucho uno cada 10 minutos por tipo: avisan sin
    llenar el .log del servicio. El resto de los logs pasa intacto."""

    def __init__(self):
        super().__init__()
        self._visto = {}

    def filter(self, record):
        if not record.name.startswith(("opentelemetry", "urllib3")):
            return True
        clave = (record.name, str(record.msg)[:120])
        ahora = time.monotonic()
        if ahora - self._visto.get(clave, -1e9) < 600:
            return False
        self._visto[clave] = ahora
        return True


def _conectar_logging(proveedor):
    nivel = logging.getLevelName(str(os.environ.get("MONITOR_NIVEL_LOGS") or "INFO").upper())
    raiz = logging.getLogger()
    # Sin handlers, Python imprime los WARNING+ por stderr (lastResort). Al
    # sumar el nuestro eso se apagaría y los .log de los servicios quedarían
    # mudos: se deja un handler de consola equivalente.
    if not raiz.handlers:
        consola = logging.StreamHandler()
        consola.setLevel(logging.WARNING)
        raiz.addHandler(consola)
    espaciado = _Espaciado()
    for handler in raiz.handlers:
        handler.addFilter(espaciado)
    raiz.addHandler(_ManejadorLogs(proveedor, nivel if isinstance(nivel, int) else logging.INFO))
    _log.setLevel(logging.INFO)   # los eventos propios salen aunque la raíz esté en WARNING


def _metricas(medidor, carpeta):
    from opentelemetry.metrics import Observation

    def latido(_):
        yield Observation(1)

    medidor.create_observable_gauge(
        "monitor.servicio.activo", callbacks=[latido],
        description="Latido: 1 mientras el servicio está vivo")
    medidor.create_observable_gauge(
        "monitor.proceso.uptime", unit="s", description="Segundos desde que arrancó el proceso",
        callbacks=[lambda _: [Observation(round(time.time() - _inicio))]])

    def disco(_):
        try:
            uso = shutil.disk_usage(carpeta)
            return [Observation(uso.free, {"tipo": "libre"}),
                    Observation(round(100 * uso.used / uso.total, 1), {"tipo": "uso_pct"})]
        except OSError:
            return []

    medidor.create_observable_gauge(
        "monitor.disco", description="Disco de la app: bytes libres (tipo=libre) y % usado (tipo=uso_pct)",
        callbacks=[disco])

    try:
        import psutil
    except ImportError:
        return
    proceso = psutil.Process()
    proceso.cpu_percent(None)   # la primera lectura siempre da 0

    def recursos(_):
        try:
            with proceso.oneshot():
                return [Observation(proceso.memory_info().rss, {"tipo": "memoria"}),
                        Observation(proceso.cpu_percent(None), {"tipo": "cpu_pct"}),
                        Observation(proceso.num_threads(), {"tipo": "hilos"})]
        except Exception:  # noqa: BLE001
            return []

    def host(_):
        try:
            return [Observation(psutil.virtual_memory().percent, {"tipo": "memoria_pct"}),
                    Observation(psutil.cpu_percent(None), {"tipo": "cpu_pct"})]
        except Exception:  # noqa: BLE001
            return []

    medidor.create_observable_gauge(
        "monitor.proceso", description="Proceso: memoria (bytes), cpu_pct e hilos", callbacks=[recursos])
    medidor.create_observable_gauge(
        "monitor.host", description="Máquina: memoria_pct y cpu_pct", callbacks=[host])


def _instrumentar(app, excluir, host_destino):
    instrumentado = []
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls=",".join([*_EXCLUIR_SIEMPRE, *excluir]),
            exclude_spans=["receive", "send"],
            server_request_hook=_marca_de_la_request,
        )
        instrumentado.append("fastapi")
    # Clientes que haya en la app: HTTP saliente (IA, planillas, APIs) y el
    # Postgres del hub. Sin la librería o sin su instrumentación, se saltea.
    for libreria, modulo, clase, opciones in (
        ("httpx", "opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor", {}),
        ("requests", "opentelemetry.instrumentation.requests", "RequestsInstrumentor",
         {"excluded_urls": re.escape(host_destino)} if host_destino else {}),
        ("psycopg2", "opentelemetry.instrumentation.psycopg2", "Psycopg2Instrumentor",
         {"enable_commenter": False}),
    ):
        if importlib.util.find_spec(libreria) is None:
            continue
        try:
            getattr(importlib.import_module(modulo), clase)().instrument(**opciones)
            instrumentado.append(libreria)
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            print(f"[monitor] no se pudo instrumentar {libreria}: {exc!r}", file=sys.stderr)
    _estado["instrumentado"] = instrumentado


# parámetros de la URL cuyo valor nunca sale del servidor (tokens, claves)
_SENSIBLES = re.compile(r"reset|token|key|clave|pass|pwd|secret|sig|code|auth|credential|sesion|session", re.I)


def _redactar_query(consulta: str) -> str:
    partes = []
    for par in consulta.split("&"):
        clave, igual, _ = par.partition("=")
        partes.append(f"{clave}=REDACTADO" if igual and _SENSIBLES.search(unquote(clave)) else par)
    return "&".join(partes)


def _marca_de_la_request(span, scope):
    """Las apps reciben la marca del hub en ?marca=: queda en la traza para
    poder filtrar errores y demoras por marca. De paso se tapa el valor de
    los parámetros sensibles de la query."""
    try:
        if span is None or not span.is_recording():
            return
        consulta = (scope.get("query_string") or b"").decode("latin-1")
        if not consulta:
            return
        limpia = _redactar_query(consulta)
        if limpia != consulta:
            span.set_attribute("url.query", limpia)
        m = re.search(r"(?:^|&)marca=([^&]*)", consulta)
        if m and m.group(1):
            span.set_attribute("marca", _recortar(unquote(m.group(1).replace("+", " ")), 120))
    except Exception:  # noqa: BLE001
        pass


def _enganchar_excepciones():
    """Errores que nadie atrapa: en hilos (jobs, lectores de casillas) y los
    que tiran abajo el proceso. Se registran y después sigue el comportamiento
    de siempre."""
    hook_hilos = threading.excepthook

    def en_hilo(args):
        if args.exc_type is not SystemExit:
            registrar_error(args.exc_value, origen="hilo",
                            hilo=getattr(args.thread, "name", "") or "")
        hook_hilos(args)

    threading.excepthook = en_hilo
    hook_proceso = sys.excepthook

    def en_proceso(tipo, valor, tb):
        if not issubclass(tipo, KeyboardInterrupt):
            registrar_error(valor, origen="proceso", fatal=True)
            _cerrar()
        hook_proceso(tipo, valor, tb)

    sys.excepthook = en_proceso


def _cerrar():
    """Vacía lo pendiente antes de salir (atexit o caída del proceso)."""
    while _proveedores:
        proveedor = _proveedores.pop()
        try:
            proveedor.shutdown()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------- API pública
def activa() -> bool:
    return bool(_estado["activo"])


def estado() -> dict:
    """Para /api/diagnostico: qué ve el proceso, sin exponer la clave."""
    clave = os.environ.get("NEW_RELIC_LICENSE_KEY") or ""
    return {
        **{k: v for k, v in _estado.items()},
        "modulo": VERSION_MODULO,
        "NEW_RELIC_LICENSE_KEY": {
            "presente": bool(clave.strip()),
            "longitud": len(clave),
            "espacios_al_borde": clave != clave.strip(),
        },
        "MONITOR_GRUPO_presente": bool(os.environ.get("MONITOR_GRUPO", "").strip()),
    }


@contextmanager
def tramo(nombre: str, *, tipo: str = "interno", ignorar=(), **atributos):
    """Mide un bloque como un tramo de la traza actual (o una traza nueva).
    Si el bloque lanza una excepción, queda registrada y se relanza.
    tipo: "interno" | "cliente" (llamada a otro sistema: FBS, IA, APIs).
    ignorar: excepciones esperadas que no cuentan como error del tramo."""
    if not _estado["activo"] or _tracer is None:
        yield _TRAMO_NULO
        return
    from opentelemetry.trace import SpanKind, Status, StatusCode
    kind = SpanKind.CLIENT if tipo == "cliente" else SpanKind.INTERNAL
    with _tracer.start_as_current_span(nombre, kind=kind, attributes=_limpiar(atributos),
                                       record_exception=False, set_status_on_exception=False) as span:
        try:
            yield span
        except Exception as exc:
            if not isinstance(exc, tuple(ignorar)):
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, _recortar(f"{type(exc).__name__}: {exc}", 300)))
            raise


def registrar_error(exc: BaseException = None, mensaje: str = None, *, log: bool = True, **contexto):
    """Registra un error que la app manejó (o no): queda en la traza con su
    stack, suma a la métrica monitor.errores y sale como log de error.
    Sin excepción explícita toma la que se está manejando (dentro de un except).
    log=False cuando la app ya lo dejó en su log (log.exception): no se duplica."""
    if exc is None:
        exc = sys.exc_info()[1]
    if exc is None or not _estado["activo"]:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode
        atributos = _limpiar(contexto)
        origen = str(contexto.get("origen") or "app")
        if _contador_errores is not None:
            _contador_errores.add(1, {"origen": origen, "error.type": type(exc).__name__})
        descripcion = _recortar(f"{type(exc).__name__}: {exc}", 300)

        def anotar(span):
            span.record_exception(exc, attributes=atributos)
            span.set_status(Status(StatusCode.ERROR, descripcion))
            # dentro del tramo, así el log queda enlazado a la traza del error
            if log:
                _log.error(mensaje or descripcion, exc_info=(type(exc), exc, exc.__traceback__),
                           extra={"monitor": {**atributos, "error.type": type(exc).__name__}})

        actual = trace.get_current_span()
        if actual.is_recording():
            anotar(actual)
        else:
            with _tracer.start_as_current_span(f"error {origen}", attributes=atributos,
                                               record_exception=False,
                                               set_status_on_exception=False) as nuevo:
                anotar(nuevo)
    except Exception:  # noqa: BLE001 — el monitoreo nunca rompe la app
        pass


def evento(mensaje: str, nivel: int = logging.INFO, **atributos):
    """Deja un evento de negocio o de ciclo de vida como log (con atributos)."""
    if not _estado["activo"]:
        return
    try:
        _log.log(nivel, mensaje, extra={"monitor": _limpiar(atributos)})
    except Exception:  # noqa: BLE001
        pass
