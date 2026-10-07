# -*- coding: utf-8 -*-
"""Seguridad de acceso del conciliador: autenticación, IP del cliente y
protección básica contra fuerza bruta y CSRF.

Criterio (el mismo del compensador y del resto del grupo):

* APP_PASSWORD define la contraseña de la app. Sin ella, la app NO se abre:
  solo atiende a quien llega directo por loopback (127.0.0.1) sin pasar por
  ningún proxy; todo acceso remoto (Railway, túnel, red local, hub) se
  rechaza con 503.
* Con APP_PASSWORD, toda ruta (HTML, API, estáticos) exige una cookie de
  sesión firmada con vencimiento, salvo /login y /health.
* La cookie va firmada con HMAC-SHA256. La clave sale de SESSION_SECRET o,
  si falta, se deriva de APP_PASSWORD (cambiar la contraseña invalida todas
  las sesiones).
* La IP del cliente NUNCA se toma de X-Forwarded-For a ciegas: ver
  ip_cliente().
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
from urllib.parse import urlsplit

COOKIE = "conciliador_sesion"
_LOGIN_PUBLICOS = ("/login", "/health", "/favicon.ico")

# Encabezados que solo existen si el pedido pasó por algún proxy/túnel/hub:
# si aparece alguno, el pedido NO es "local" aunque el socket sea loopback.
_ENCABEZADOS_PROXY = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
                      "x-forwarded-port", "forwarded", "x-real-ip",
                      "cf-connecting-ip", "cf-ray", "x-orbit-usuario",
                      "x-orbit-permisos")


# ---- configuración ---------------------------------------------------------

def clave_app() -> str:
    return os.environ.get("APP_PASSWORD", "")


def _num(nombre: str, defecto: float) -> float:
    try:
        return float(os.environ.get(nombre, "") or defecto)
    except ValueError:
        return float(defecto)


def sesion_horas() -> float:
    """Vida de la sesión sin uso (se renueva con la actividad)."""
    return max(0.1, _num("SESION_HORAS", 12))


def sesion_max_dias() -> float:
    """Tope absoluto desde el login, aunque haya actividad."""
    return max(0.1, _num("SESION_MAX_DIAS", 7))


def _secreto() -> bytes:
    base = os.environ.get("SESSION_SECRET") or clave_app()
    return hmac.new(base.encode("utf-8"), b"conciliador-sesion-v1",
                    hashlib.sha256).digest()


# ---- IP del cliente --------------------------------------------------------

def es_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def _ip_valida(texto: str) -> str | None:
    try:
        return str(ipaddress.ip_address((texto or "").strip()))
    except ValueError:
        return None


def modo_proxy() -> str:
    """Quién está delante de la app, porque de eso depende qué encabezado de
    IP es confiable:

    railway     el borde de Railway agrega la IP real AL FINAL de
                X-Forwarded-For (lo que viene antes lo puede inventar el
                cliente): se toma el último elemento.
    cloudflare  túnel de Cloudflare en la misma máquina (Nave): el túnel
                conecta por loopback y pone CF-Connecting-IP. Se confía en
                ese encabezado SOLO si el socket es loopback.
    none        sin proxy: vale la IP del socket.

    PROXY_MODE lo fuerza; sin él, Railway se detecta por sus variables y el
    resto se trata como cloudflare/loopback."""
    explicito = os.environ.get("PROXY_MODE", "").strip().lower()
    if explicito in ("railway", "cloudflare", "none"):
        return explicito
    if os.environ.get("RAILWAY_PROJECT_ID") or os.environ.get("RAILWAY_ENVIRONMENT"):
        return "railway"
    return "cloudflare"


def ip_cliente(request) -> str:
    """IP del cliente para límites de intentos y auditoría. No se confía en
    el primer valor de X-Forwarded-For (lo inventa el atacante)."""
    peer = request.client.host if request.client else ""
    modo = modo_proxy()
    if modo == "railway":
        for valor in reversed(request.headers.get("x-forwarded-for", "").split(",")):
            ip = _ip_valida(valor)
            if ip:
                return ip
        return peer
    if modo == "cloudflare" and es_loopback(peer):
        ip = _ip_valida(request.headers.get("cf-connecting-ip", ""))
        if ip:
            return ip
    return peer


def es_local(request) -> bool:
    """True solo si el pedido llegó directo por loopback, sin proxy de por
    medio (alguien en la propia máquina)."""
    peer = request.client.host if request.client else ""
    if not es_loopback(peer):
        return False
    return not any(h in request.headers for h in _ENCABEZADOS_PROXY)


# ---- sesión firmada --------------------------------------------------------

def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).decode("ascii").rstrip("=")


def _desb64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def emitir_sesion(ahora: float | None = None, inicio: float | None = None) -> str:
    ahora = time.time() if ahora is None else ahora
    inicio = ahora if inicio is None else inicio
    exp = min(ahora + sesion_horas() * 3600, inicio + sesion_max_dias() * 86400)
    cuerpo = _b64(json.dumps({"iat": int(inicio), "exp": int(exp)},
                             separators=(",", ":")).encode())
    firma = _b64(hmac.new(_secreto(), cuerpo.encode("ascii"), hashlib.sha256).digest())
    return f"{cuerpo}.{firma}"


def leer_sesion(valor: str, ahora: float | None = None) -> dict | None:
    """Devuelve {'iat','exp'} si la cookie es auténtica y no venció."""
    if not clave_app() or not valor or "." not in valor:
        return None
    cuerpo, _, firma = valor.partition(".")
    try:
        esperada = _b64(hmac.new(_secreto(), cuerpo.encode("ascii"),
                                 hashlib.sha256).digest())
        if not hmac.compare_digest(firma.encode("ascii"), esperada.encode("ascii")):
            return None
        datos = json.loads(_desb64(cuerpo))
        ahora = time.time() if ahora is None else ahora
        if ahora >= float(datos["exp"]) or ahora >= float(datos["iat"]) + sesion_max_dias() * 86400:
            return None
        return datos
    except (ValueError, KeyError, TypeError, UnicodeError):
        return None


def clave_correcta(candidata: str) -> bool:
    esperada = clave_app()
    if not esperada:
        return False
    return secrets.compare_digest((candidata or "").encode("utf-8"),
                                  esperada.encode("utf-8"))


# ---- límite de intentos de login ------------------------------------------

class LimiteIntentos:
    """Ventana deslizante en memoria: máx. `maximo` fallos por clave en
    `ventana` segundos. Hay además un tope global para que cambiar de IP no
    dé intentos infinitos."""

    def __init__(self, maximo=10, ventana=900, maximo_global=60):
        self.maximo, self.ventana, self.maximo_global = maximo, ventana, maximo_global
        self._fallos: dict[str, list[float]] = {}
        self._global: list[float] = []
        self._lock = threading.Lock()

    def _limpiar(self, lista, ahora):
        while lista and ahora - lista[0] > self.ventana:
            lista.pop(0)

    def bloqueado(self, clave: str, ahora: float | None = None) -> int:
        """Segundos que faltan para poder reintentar (0 = permitido)."""
        ahora = time.time() if ahora is None else ahora
        with self._lock:
            propio = self._fallos.get(clave, [])
            self._limpiar(propio, ahora)
            self._limpiar(self._global, ahora)
            for lista, tope in ((propio, self.maximo), (self._global, self.maximo_global)):
                if len(lista) >= tope:
                    return int(self.ventana - (ahora - lista[0])) + 1
            return 0

    def fallo(self, clave: str, ahora: float | None = None):
        ahora = time.time() if ahora is None else ahora
        with self._lock:
            self._fallos.setdefault(clave, []).append(ahora)
            self._global.append(ahora)
            if len(self._fallos) > 5000:      # no crecer sin límite
                self._fallos = {k: v for k, v in self._fallos.items()
                                if v and ahora - v[-1] <= self.ventana}

    def ok(self, clave: str):
        with self._lock:
            self._fallos.pop(clave, None)


LIMITE_LOGIN = LimiteIntentos()


# ---- CSRF -------------------------------------------------------------------

def origen_valido(request) -> bool:
    """Para métodos que modifican: si el navegador manda Origin, su host debe
    ser el del propio servicio (o el host reenviado por el hub/túnel). Sin
    Origin (curl, scripts, mismo origen en algunos navegadores) se acepta:
    la cookie SameSite=Lax ya impide el envío cross-site de POST."""
    origen = request.headers.get("origin")
    if not origen or origen == "null":
        return origen != "null"
    host = urlsplit(origen).netloc.lower()
    validos = {request.headers.get("host", "").lower(),
               request.headers.get("x-forwarded-host", "").split(",")[0].strip().lower()}
    extra = os.environ.get("ORIGENES_PERMITIDOS", "")
    validos |= {urlsplit(o if "//" in o else "//" + o).netloc.lower()
                for o in extra.split(",") if o.strip()}
    validos.discard("")
    return host in validos


def es_https(request) -> bool:
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def es_publica(path: str) -> bool:
    return path in _LOGIN_PUBLICOS
