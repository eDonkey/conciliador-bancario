# -*- coding: utf-8 -*-
"""Service telemetry (monitoring): errors, traces, metrics and logs.

Sends everything through OpenTelemetry to New Relic (or any OTLP/HTTP backend)
with the GROUP and the SERVICE on every item, so one dashboard and one set of
alerts serve every group where the apps are installed.

SINGLE SOURCE: monitor/python/telemetria.py. Each app has an identical copy in
engine/telemetria.py (app/telemetria.py in the savings-plan API,
src/sga_mcp/telemetria.py in its MCP) that is updated with
`python monitor/sincronizar.py`.
Don't edit an app's copy: the next sync overwrites it.

The public API keeps its Spanish names (every app calls it): iniciar, tramo,
registrar_error, anotar, evento, envolver_asgi, activa, estado. Everything the
module sends (attribute keys and values, span, metric and log names) is a
contract with the dashboard and the alerts and stays as it is.

Usage (app.py, right after creating the app):

    from engine import telemetria
    app = FastAPI(...)
    telemetria.iniciar(app, "conciliador", puerto=8765)

Errors the app handles (they don't arrive as 500s) but that must still be seen:

    except Exception as exc:
        telemetria.registrar_error(exc, origen="job", job=job_id)

Custom spans, to measure what matters (FBS queries, jobs, AI):

    with telemetria.tramo("FBS consulta", tipo="cliente", marca=marca) as t:
        filas = ...
        t.set_attribute("fbs.filas", len(filas))

Variables (from the environment or the .env next to app.py, never overriding the environment):

    NEW_RELIC_LICENSE_KEY   ingest license key: turns on sending to New Relic
    NEW_RELIC_REGION        us (default) | eu
    OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_HEADERS
                            another OTLP/HTTP backend (Grafana Cloud, SigNoz...)
    MONITOR_GRUPO           group or client that owns the installation (nave-motors)
    MONITOR_ENTORNO         produccion | demo | desarrollo
    MONITOR_DESPLIEGUE      on-premise | railway (detected automatically)
    MONITOR_MUESTREO        fraction of traces kept (1 = all)
    MONITOR_NIVEL_LOGS      minimum level of the logs that are sent (INFO)
    MONITOR_INTERVALO       seconds between metric exports (60)
    MONITOR_ACTIVO=0        turns telemetry off even with a destination

It never breaks the app: without a destination, without the packages installed
or on any failure of its own it stays in null mode and the app runs exactly the same.
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

VERSION_MODULO = "2026.10.05"

# the app's folder (this file lives in <app>/engine/)
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Not worth tracing: static files, the hub watchdog's check and the icon.
# Regexes joined by commas (don't use commas inside a regex).
_ALWAYS_EXCLUDED = (
    r"/static/",
    r"\.(?:js|css|map|png|jpe?g|gif|svg|ico|woff2?|ttf)(?:\?|$)",
    r"favicon",
    r"/__monitor",
)

# What estado() reports; its keys are part of the apps' /api/diagnostico.
_state = {"activo": False, "motivo": "sin iniciar"}
_tracer = None
_error_counter = None
_providers = []
_start_time = time.time()
_log = logging.getLogger("monitor")


class _NullSpan:
    """What tramo() returns without telemetry: accepts everything and does nothing."""

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


_NULL_SPAN = _NullSpan()


# ------------------------------------------------------------------ helpers
def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "on", "si", "sí", "yes")


def _slug(text) -> str:
    t = unicodedata.normalize("NFD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")


def _truncate(text, length=1000) -> str:
    text = str(text)
    return text if len(text) <= length else text[: length - 1] + "…"


_URL_WITH_QUERY = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s?#\"'<>]*)\?[^\s#\"'<>]*")


def _strip_url_queries(text: str) -> str:
    """Removes the query of the URLs in a log message: httpx logs every request at
    INFO with its full URL ("HTTP Request: GET https://api/x?documento=..."), and
    searches, documents and tokens travel there."""
    return _URL_WITH_QUERY.sub(r"\1", text) if "://" in text else text


def _clean_attributes(attributes: dict) -> dict:
    """Attributes OpenTelemetry accepts: scalars or lists of scalars."""
    cleaned = {}
    for key, value in (attributes or {}).items():
        if value is None:
            continue
        if isinstance(value, (bool, int, float)):
            cleaned[str(key)] = value
        elif isinstance(value, (list, tuple, set)):
            cleaned[str(key)] = [_truncate(v, 300) for v in list(value)[:50]]
        else:
            cleaned[str(key)] = _truncate(value)
    return cleaned


def _load_local_env():
    """Adds the monitoring variables of the app's .env, without overriding the
    environment. Only MONITOR_*, NEW_RELIC_* and OTEL_*: each app loads the rest its own way."""
    try:
        with open(os.path.join(_APP_ROOT, ".env"), encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if not m or not m.group(1).startswith(("MONITOR_", "NEW_RELIC_", "OTEL_")):
            continue
        value = m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(m.group(1), value)


def _git_version():
    """(branch, short commit) of what is running, reading .git without calling
    git. On Railway they come in deploy variables."""
    branch = os.environ.get("RAILWAY_GIT_BRANCH", "")
    commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7]
    if commit:
        return branch, commit
    try:
        git = os.path.join(_APP_ROOT, ".git")
        if os.path.isfile(git):                      # worktree: "gitdir: <path>"
            with open(git, encoding="utf-8") as f:
                git = os.path.normpath(os.path.join(_APP_ROOT, f.read().split(":", 1)[1].strip()))
        common = git
        if os.path.isfile(os.path.join(git, "commondir")):
            with open(os.path.join(git, "commondir"), encoding="utf-8") as f:
                common = os.path.normpath(os.path.join(git, f.read().strip()))
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
        if not head.startswith("ref:"):
            return "", head[:7]                      # detached HEAD
        ref = head[4:].strip()
        branch = ref.split("refs/heads/", 1)[-1]
        ref_path = os.path.join(common, *ref.split("/"))
        if os.path.isfile(ref_path):
            with open(ref_path, encoding="utf-8") as f:
                commit = f.read().strip()[:7]
        elif os.path.isfile(os.path.join(common, "packed-refs")):
            with open(os.path.join(common, "packed-refs"), encoding="utf-8") as f:
                for line in f:
                    if line.rstrip().endswith(" " + ref):
                        commit = line.split()[0][:7]
                        break
    except (OSError, IndexError):
        pass
    return branch, commit


def _port(default) -> str:
    if os.environ.get("PORT"):
        return os.environ["PORT"].strip()
    args = sys.argv
    for i, arg in enumerate(args):
        if arg == "--port" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--port="):
            return arg.split("=", 1)[1]
    return str(default or "")


def _environment() -> str:
    value = _slug(os.environ.get("MONITOR_ENTORNO", ""))
    if value:
        return value
    # every app has its own demo flag (DEMO_MODE, PANEL_DEMO, COMPENSADOR_DEMO_MODE...)
    if any(_truthy(v) for k, v in os.environ.items()
           if k == "DEMO_MODE" or k.endswith(("_DEMO", "_DEMO_MODE"))):
        return "demo"
    return "produccion"


def _deployment() -> str:
    value = _slug(os.environ.get("MONITOR_DESPLIEGUE", ""))
    if value:
        return value
    if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"):
        return "railway"
    return "on-premise" if os.name == "nt" else "servidor"


def _parse_headers(text: str) -> dict:
    """OTEL_EXPORTER_OTLP_HEADERS: "key=value,key2=value2" (url-encoded)."""
    headers = {}
    for part in str(text or "").split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            if key.strip():
                headers[unquote(key.strip())] = unquote(value.strip())
    return headers


def _destination():
    """(base url, headers, name) of the OTLP/HTTP backend; None if there is none."""
    key = (os.environ.get("NEW_RELIC_LICENSE_KEY") or "").strip().strip("\"'")
    base = (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    if base:
        headers = _parse_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""))
        host = urlparse(base).hostname or ""
        is_new_relic = host == "nr-data.net" or host.endswith(".nr-data.net")
        # the license key only goes to New Relic, never to another backend
        if key and is_new_relic and not any(k.lower() == "api-key" for k in headers):
            headers["api-key"] = key
        return base.rstrip("/"), headers, "new-relic" if is_new_relic else "otlp"
    if key:
        eu = os.environ.get("NEW_RELIC_REGION", "").strip().lower() == "eu"
        base = "https://otlp.eu01.nr-data.net" if eu else "https://otlp.nr-data.net"
        return base, {"api-key": key}, "new-relic"
    return None


def _severity(level: int):
    from opentelemetry._logs import SeverityNumber
    if level >= logging.CRITICAL:
        return SeverityNumber.FATAL, "FATAL"
    if level >= logging.ERROR:
        return SeverityNumber.ERROR, "ERROR"
    if level >= logging.WARNING:
        return SeverityNumber.WARN, "WARN"
    if level >= logging.INFO:
        return SeverityNumber.INFO, "INFO"
    return SeverityNumber.DEBUG, "DEBUG"


class _LogHandler(logging.Handler):
    """Sends Python logs as OTLP logs, with the trace and the exception."""

    def __init__(self, provider, level):
        super().__init__(level=level)
        self._provider = provider
        self._local = threading.local()

    def emit(self, record):
        # the exporter's own logs (exporter, urllib3) are not re-sent: with the
        # backend down that would be a loop
        if record.name.startswith(("opentelemetry", "urllib3")) or getattr(self._local, "inside", False):
            return
        self._local.inside = True
        try:
            try:
                body = record.getMessage()
            except Exception:  # noqa: BLE001 — a malformed log still goes out
                body = str(record.msg)
            attributes = {
                "logger.name": record.name,
                "code.function.name": record.funcName,
                "code.file.path": record.pathname,
                "code.line.number": record.lineno,
                "thread.name": record.threadName,
            }
            extra = getattr(record, "monitor", None)
            if isinstance(extra, dict):
                attributes.update(extra)
            number, text = _severity(record.levelno)
            exception = record.exc_info[1] if record.exc_info else None
            self._provider.get_logger(record.name).emit(
                timestamp=int(record.created * 1e9),
                severity_number=number,
                severity_text=text,
                body=_truncate(_strip_url_queries(body), 8000),
                attributes=_clean_attributes(attributes),
                exception=exception,
            )
        except Exception:  # noqa: BLE001 — monitoring never breaks the app
            pass
        finally:
            self._local.inside = False


# ------------------------------------------------------------------- startup
def iniciar(app=None, servicio: str = "", *, puerto=None, excluir=(), carpeta_datos=None) -> bool:
    """Configures the process's telemetry. Returns True if it ended up active.

    app            the FastAPI app (None for processes without HTTP)
    servicio       short, fixed name of the service: conciliador, ventas...
    puerto         the app's default port (if it doesn't come in PORT or --port)
    excluir        extra regexes of URLs that aren't traced (polling, e.g.)
    carpeta_datos  folder whose disk is watched (the app's by default)
    """
    if _state["activo"]:
        return True
    try:
        _load_local_env()
        service = _slug(servicio) or "app"
        group = _slug(os.environ.get("MONITOR_GRUPO", "")) or "sin-grupo"
        environment = _environment()
        deployment = _deployment()
        branch, commit = _git_version()
        version = f"{branch}@{commit}" if branch and commit else (commit or "sin-version")
        host = socket.gethostname()
        instance = f"{host}:{_port(puerto) or 'pid' + str(os.getpid())}"
        _state.update({
            "servicio": service, "grupo": group, "nombre": f"{group}/{service}",
            "entorno": environment, "despliegue": deployment, "version": version,
            "instancia": instance,
        })

        if not _truthy(os.environ.get("MONITOR_ACTIVO", "1")):
            _state["motivo"] = "apagada con MONITOR_ACTIVO=0"
            return False
        destination = _destination()
        if not destination:
            _state["motivo"] = "sin destino: falta NEW_RELIC_LICENSE_KEY (u OTEL_EXPORTER_OTLP_ENDPOINT)"
            return False
        base, headers, backend = destination
        _state["destino"] = f"{backend} ({urlparse(base).hostname})"

        # OpenTelemetry's stable HTTP conventions (the ones the dashboards use)
        os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
        try:
            _configure(app, base, headers, excluir, carpeta_datos, {
                "service.name": f"{group}/{service}",
                "service.namespace": group,
                "service.version": version,
                "service.instance.id": instance,
                "deployment.environment": environment,
                "deployment.environment.name": environment,
                "host.name": host,
                # "tags.*" attributes become entity tags in New Relic: they
                # filter dashboards, alerts and workloads by group
                "tags.grupo": group,
                "tags.servicio": service,
                "tags.entorno": environment,
                "tags.despliegue": deployment,
                "tags.rama": branch or "-",
            })
        except ImportError as exc:
            _state["motivo"] = f"faltan paquetes de OpenTelemetry ({exc.name}): pip install -r requirements.txt"
            print(f"[monitor] telemetría desactivada: {_state['motivo']}", file=sys.stderr)
            return False

        _state.update({"activo": True, "motivo": ""})
        evento(f"Servicio iniciado: {group}/{service} {version} en {instance} ({deployment}, {environment})",
               evento="inicio")
        return True
    except Exception as exc:  # noqa: BLE001 — monitoring never breaks the app
        _state.update({"activo": False, "motivo": f"error al iniciar: {exc!r}"})
        print(f"[monitor] telemetría desactivada: {exc!r}", file=sys.stderr)
        return False


def _configure(app, base, headers, excluded, data_folder, attributes):
    global _tracer, _error_counter
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
        # imported here so that, if it's missing, nothing is left half configured
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: F401

    resource = Resource.create(attributes)
    common = {"headers": headers, "timeout": 5, "compression": Compression.Gzip}

    try:
        sampling = min(1.0, max(0.0, float(os.environ.get("MONITOR_MUESTREO") or 1)))
    except ValueError:
        sampling = 1.0
    tracer_provider = TracerProvider(resource=resource, sampler=ParentBased(TraceIdRatioBased(sampling)))
    tracer_provider.add_span_processor(_without_orphan_queries(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=base + "/v1/traces", **common))))
    trace.set_tracer_provider(tracer_provider)
    _tracer = trace.get_tracer("monitor")

    # New Relic asks for delta temporality for counters and histograms
    delta, cumulative = AggregationTemporality.DELTA, AggregationTemporality.CUMULATIVE
    try:
        interval = max(5.0, float(os.environ.get("MONITOR_INTERVALO") or 60))
    except ValueError:
        interval = 60.0
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=base + "/v1/metrics", preferred_temporality={
            Counter: delta, Histogram: delta, ObservableCounter: delta,
            UpDownCounter: cumulative, ObservableUpDownCounter: cumulative, ObservableGauge: cumulative,
        }, **common),
        export_interval_millis=interval * 1000,
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=base + "/v1/logs", **common)))
    set_logger_provider(logger_provider)

    _providers.extend([meter_provider, logger_provider, tracer_provider])
    atexit.register(_shutdown)

    _connect_logging(logger_provider)
    _register_metrics(metrics.get_meter("monitor"), data_folder or _APP_ROOT)
    _error_counter = metrics.get_meter("monitor").create_counter(
        "monitor.errores", unit="{error}", description="Errores registrados por el servicio")
    _instrument(app, excluded, urlparse(base).hostname or "")
    _hook_uncaught_exceptions()


class _ThrottleExporterNotices(logging.Filter):
    """The exporter's own notices (backend down, no internet, invalid key) reach
    the console at most once every 10 minutes per kind: they warn without
    filling the service's .log. Every other log passes untouched."""

    def __init__(self):
        super().__init__()
        self._seen = {}

    def filter(self, record):
        if not record.name.startswith(("opentelemetry", "urllib3")):
            return True
        key = (record.name, str(record.msg)[:120])
        now = time.monotonic()
        if now - self._seen.get(key, -1e9) < 600:
            return False
        self._seen[key] = now
        return True


def _without_orphan_queries(inner):
    """Wraps the span processor: database queries that hang from no request or
    span (startup, migrations, periodic tasks) are not sent. They would be
    thousands of single-query traces without context."""
    from opentelemetry.sdk.trace import SpanProcessor
    db_scopes = ("opentelemetry.instrumentation.psycopg", "opentelemetry.instrumentation.dbapi")

    class _WithoutOrphanQueries(SpanProcessor):
        def on_start(self, span, parent_context=None):
            inner.on_start(span, parent_context=parent_context)

        def _on_ending(self, span):
            if hasattr(inner, "_on_ending"):
                inner._on_ending(span)

        def on_end(self, span):
            scope = getattr(getattr(span, "instrumentation_scope", None), "name", "") or ""
            if span.parent is None and scope.startswith(db_scopes):
                return
            inner.on_end(span)

        def shutdown(self):
            inner.shutdown()

        def force_flush(self, timeout_millis=30000):
            return inner.force_flush(timeout_millis)

    return _WithoutOrphanQueries()


def _connect_logging(provider):
    level = logging.getLevelName(str(os.environ.get("MONITOR_NIVEL_LOGS") or "INFO").upper())
    root = logging.getLogger()
    # Without handlers, Python prints WARNING+ to stderr (lastResort). Adding
    # ours would turn that off and leave the services' .log files silent:
    # an equivalent console handler is added.
    if not root.handlers:
        console = logging.StreamHandler()
        console.setLevel(logging.WARNING)
        root.addHandler(console)
    throttle = _ThrottleExporterNotices()
    for handler in root.handlers:
        handler.addFilter(throttle)
    root.addHandler(_LogHandler(provider, level if isinstance(level, int) else logging.INFO))
    _log.setLevel(logging.INFO)   # our own events go out even if the root is at WARNING


def _register_metrics(meter, folder):
    from opentelemetry.metrics import Observation

    def heartbeat(_):
        yield Observation(1)

    meter.create_observable_gauge(
        "monitor.servicio.activo", callbacks=[heartbeat],
        description="Latido: 1 mientras el servicio está vivo")
    meter.create_observable_gauge(
        "monitor.proceso.uptime", unit="s", description="Segundos desde que arrancó el proceso",
        callbacks=[lambda _: [Observation(round(time.time() - _start_time))]])

    def disk(_):
        try:
            usage = shutil.disk_usage(folder)
            return [Observation(usage.free, {"tipo": "libre"}),
                    Observation(round(100 * usage.used / usage.total, 1), {"tipo": "uso_pct"})]
        except OSError:
            return []

    meter.create_observable_gauge(
        "monitor.disco", description="Disco de la app: bytes libres (tipo=libre) y % usado (tipo=uso_pct)",
        callbacks=[disk])

    try:
        import psutil
    except ImportError:
        return
    process = psutil.Process()
    process.cpu_percent(None)   # the first reading is always 0

    def process_resources(_):
        try:
            with process.oneshot():
                return [Observation(process.memory_info().rss, {"tipo": "memoria"}),
                        Observation(process.cpu_percent(None), {"tipo": "cpu_pct"}),
                        Observation(process.num_threads(), {"tipo": "hilos"})]
        except Exception:  # noqa: BLE001
            return []

    def host_resources(_):
        try:
            return [Observation(psutil.virtual_memory().percent, {"tipo": "memoria_pct"}),
                    Observation(psutil.cpu_percent(None), {"tipo": "cpu_pct"})]
        except Exception:  # noqa: BLE001
            return []

    meter.create_observable_gauge(
        "monitor.proceso", description="Proceso: memoria (bytes), cpu_pct e hilos", callbacks=[process_resources])
    meter.create_observable_gauge(
        "monitor.host", description="Máquina: memoria_pct y cpu_pct", callbacks=[host_resources])


def _instrument(app, excluded, backend_host):
    instrumented = []
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls=",".join([*_ALWAYS_EXCLUDED, *excluded]),
            exclude_spans=["receive", "send"],
            server_request_hook=_server_request_hook,
        )
        instrumented.append("fastapi")
    # Clients present in the app: outbound HTTP (AI, spreadsheets, APIs) and
    # Postgres. Without the library or its instrumentation, it's skipped.
    requests_options = {"request_hook": _strip_outbound_query}
    if backend_host:
        requests_options["excluded_urls"] = re.escape(backend_host)
    for library, module, class_name, options in (
        ("httpx", "opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor",
         {"request_hook": _strip_outbound_query, "async_request_hook": _strip_outbound_query_async}),
        ("requests", "opentelemetry.instrumentation.requests", "RequestsInstrumentor", requests_options),
        ("psycopg2", "opentelemetry.instrumentation.psycopg2", "Psycopg2Instrumentor",
         {"enable_commenter": False}),
        ("psycopg", "opentelemetry.instrumentation.psycopg", "PsycopgInstrumentor",
         {"enable_commenter": False}),
    ):
        if importlib.util.find_spec(library) is None:
            continue
        try:
            getattr(importlib.import_module(module), class_name)().instrument(**options)
            instrumented.append(library)
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            print(f"[monitor] no se pudo instrumentar {library}: {exc!r}", file=sys.stderr)
    _state["instrumentado"] = instrumented


def _strip_outbound_query(span, request):
    """Outbound HTTP calls are recorded without the query: tokens and sessions
    usually travel there (SGA, for example, puts the session token in the URL)."""
    try:
        if span is None or not span.is_recording():
            return
        url = str(getattr(request, "url", "") or "")
        if "?" in url:
            span.set_attribute("url.full", url.split("?", 1)[0])
    except Exception:  # noqa: BLE001
        pass


async def _strip_outbound_query_async(span, request):
    _strip_outbound_query(span, request)


# Query parameters whose value is kept: brand, dates, paging. The rest goes
# as "…": a search query carries names, documents or domains, and sometimes
# tokens (e.g. ?q=<holder>&documento=<dni> in the savings-plan API).
_VISIBLE_PARAMS = {"marca", "desde", "hasta", "periodo", "fecha", "mes", "anio", "vista", "job", "tipo",
                   "completo", "refrescar", "page", "size", "limit", "orden", "formato", "lista"}


def _safe_query(query: str) -> str:
    parts = []
    for pair in query.split("&"):
        key, equals, _ = pair.partition("=")
        parts.append(pair if not equals or unquote(key).lower() in _VISIBLE_PARAMS else f"{key}=…")
    return "&".join(parts)


def _server_request_hook(span, scope):
    """The apps get the brand from the hub in ?marca=: it goes on the trace so
    errors and latency can be filtered by brand. Also, only the values of the
    harmless query parameters are kept (see _VISIBLE_PARAMS)."""
    try:
        if span is None or not span.is_recording():
            return
        query = (scope.get("query_string") or b"").decode("latin-1")
        if not query:
            return
        safe = _safe_query(query)
        if safe != query:
            span.set_attribute("url.query", safe)
        m = re.search(r"(?:^|&)marca=([^&]*)", query)
        if m and m.group(1):
            span.set_attribute("marca", _truncate(unquote(m.group(1).replace("+", " ")), 120))
    except Exception:  # noqa: BLE001
        pass


def _hook_uncaught_exceptions():
    """Errors nobody catches: in threads (jobs, mailbox readers) and the ones
    that bring the process down. They are recorded and then the usual behavior
    follows."""
    thread_hook = threading.excepthook

    def on_thread_exception(args):
        if args.exc_type is not SystemExit:
            registrar_error(args.exc_value, origen="hilo",
                            hilo=getattr(args.thread, "name", "") or "")
        thread_hook(args)

    threading.excepthook = on_thread_exception
    process_hook = sys.excepthook

    def on_process_exception(exc_type, exc_value, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            registrar_error(exc_value, origen="proceso", fatal=True)
            _shutdown()
        process_hook(exc_type, exc_value, tb)

    sys.excepthook = on_process_exception


def _shutdown():
    """Flushes what's pending before exiting (atexit or process crash)."""
    while _providers:
        provider = _providers.pop()
        try:
            provider.shutdown()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- public API
def envolver_asgi(app, excluir=()):
    """For ASGI apps that aren't FastAPI (plain Starlette, the MCP server):
    returns the app wrapped in OpenTelemetry's middleware, which measures every
    request like in the FastAPI apps. Without telemetry, returns the same app."""
    if not _state["activo"]:
        return app
    try:
        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
    except ImportError:
        return app

    def span_details(scope):
        # fixed routes in these apps (/mcp, /salud): the path is the route
        route = scope.get("path") or "/"
        return f"{scope.get('method', 'HTTP')} {route}", {"http.route": route}

    _state.setdefault("instrumentado", []).append("asgi")
    return OpenTelemetryMiddleware(app, excluded_urls=",".join([*_ALWAYS_EXCLUDED, *excluir]),
                                   default_span_details=span_details, server_request_hook=_server_request_hook,
                                   exclude_spans=["receive", "send"])


def activa() -> bool:
    return bool(_state["activo"])


def estado() -> dict:
    """For /api/diagnostico: what the process sees, without exposing the key."""
    key = os.environ.get("NEW_RELIC_LICENSE_KEY") or ""
    return {
        **{k: v for k, v in _state.items()},
        "modulo": VERSION_MODULO,
        "NEW_RELIC_LICENSE_KEY": {
            "presente": bool(key.strip()),
            "longitud": len(key),
            "espacios_al_borde": key != key.strip(),
        },
        "MONITOR_GRUPO_presente": bool(os.environ.get("MONITOR_GRUPO", "").strip()),
    }


@contextmanager
def tramo(nombre: str, *, tipo: str = "interno", ignorar=(), sin_detalle: bool = False, **atributos):
    """Measures a block as a span of the current trace (or a new trace).
    If the block raises an exception, it is recorded and re-raised.
    tipo: "interno" | "cliente" (call to another system: FBS, AI, APIs).
    ignorar: expected exceptions that don't count as an error of the span.
    sin_detalle: don't trace what's inside (queries, HTTP): for processes that
    do thousands of operations and only matter as a whole (duration, error)."""
    if not _state["activo"] or _tracer is None:
        yield _NULL_SPAN
        return
    from contextlib import nullcontext
    from opentelemetry.trace import SpanKind, Status, StatusCode
    silence = nullcontext()
    if sin_detalle:
        try:
            from opentelemetry.instrumentation.utils import suppress_instrumentation
            silence = suppress_instrumentation()
        except ImportError:
            pass
    kind = SpanKind.CLIENT if tipo == "cliente" else SpanKind.INTERNAL
    with _tracer.start_as_current_span(nombre, kind=kind, attributes=_clean_attributes(atributos),
                                       record_exception=False, set_status_on_exception=False) as span:
        with silence:
            try:
                yield span
            except Exception as exc:
                if not isinstance(exc, tuple(ignorar)):
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, _truncate(f"{type(exc).__name__}: {exc}", 300)))
                raise


def anotar(**atributos):
    """Adds attributes to the current span (the request or the open span)."""
    if not _state["activo"]:
        return
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attributes(_clean_attributes(atributos))
    except Exception:  # noqa: BLE001
        pass


def registrar_error(exc: BaseException = None, mensaje: str = None, *, log: bool = True, **contexto):
    """Records an error the app handled (or didn't): it goes on the trace with
    its stack, adds to the monitor.errores metric and goes out as an error log.
    Without an explicit exception it takes the one being handled (inside an except).
    log=False when the app already logged it (log.exception): no duplicate."""
    if exc is None:
        exc = sys.exc_info()[1]
    if exc is None or not _state["activo"]:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode
        attributes = _clean_attributes(contexto)
        origin = str(contexto.get("origen") or "app")
        if _error_counter is not None:
            _error_counter.add(1, {"origen": origin, "error.type": type(exc).__name__})
        description = _truncate(f"{type(exc).__name__}: {exc}", 300)

        def annotate(span):
            span.record_exception(exc, attributes=attributes)
            span.set_status(Status(StatusCode.ERROR, description))
            # inside the span, so the log is linked to the error's trace
            if log:
                _log.error(mensaje or description, exc_info=(type(exc), exc, exc.__traceback__),
                           extra={"monitor": {**attributes, "error.type": type(exc).__name__}})

        current = trace.get_current_span()
        if current.is_recording():
            annotate(current)
        else:
            with _tracer.start_as_current_span(f"error {origin}", attributes=attributes,
                                               record_exception=False,
                                               set_status_on_exception=False) as new_span:
                annotate(new_span)
    except Exception:  # noqa: BLE001 — monitoring never breaks the app
        pass


def evento(mensaje: str, nivel: int = logging.INFO, **atributos):
    """Records a business or lifecycle event as a log (with attributes)."""
    if not _state["activo"]:
        return
    try:
        _log.log(nivel, mensaje, extra={"monitor": _clean_attributes(atributos)})
    except Exception:  # noqa: BLE001
        pass
