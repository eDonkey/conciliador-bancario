# -*- coding: utf-8 -*-
"""Middleware de acceso y pantalla de login del conciliador.
Ver engine/seguridad.py para el criterio."""
import html
import json
import time
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from engine import seguridad as seg

_METODOS_SEGUROS = ("GET", "HEAD", "OPTIONS")
_NEXT_PERMITIDOS = {"": "./", "diario": "diario"}

_PAGINA = """<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex">
<title>Conciliador bancario - Ingresar</title>
<style>
:root{color-scheme:dark;--acento:#38bdf8;--fondo1:#070b18;--fondo2:#0d1430;--texto:#e8ecf8;--texto2:#94a0c2;--rojo:#ff8896}
*{box-sizing:border-box;margin:0}
body{font-family:Inter,system-ui,sans-serif;background:radial-gradient(ellipse at 50% 0%,var(--fondo2),var(--fondo1) 70%) fixed;
color:var(--texto);min-height:100vh;display:grid;place-items:center;padding:16px}
main{width:100%;max-width:360px;background:rgba(255,255,255,.055);border:1px solid rgba(255,255,255,.12);
border-radius:16px;padding:28px;backdrop-filter:blur(14px)}
h1{font-family:Sora,Inter,system-ui,sans-serif;font-size:20px;margin-bottom:6px}
p{color:var(--texto2);font-size:13px;margin-bottom:18px}
label{display:block;font-size:12px;color:var(--texto2);margin-bottom:6px}
input{width:100%;padding:11px 12px;border-radius:10px;border:1px solid rgba(255,255,255,.12);
background:rgba(255,255,255,.05);color:var(--texto);font-size:14px}
input:focus{outline:none;border-color:var(--acento);box-shadow:0 0 0 3px rgba(56,189,248,.35)}
button{width:100%;margin-top:14px;padding:11px;border:0;border-radius:10px;background:var(--acento);
color:#06131f;font-weight:600;font-size:14px;cursor:pointer}
.error{margin-top:12px;color:var(--rojo);font-size:13px;min-height:18px}
</style></head><body><main>
<h1>Conciliador bancario</h1><p>Ingresá la contraseña para continuar.</p>
<form method="post" action="login__NEXT__">
<label for="clave">Contraseña</label>
<input id="clave" name="clave" type="password" autocomplete="current-password" autofocus required>
<button type="submit">Ingresar</button>
<div class="error" role="alert">__ERROR__</div>
</form></main></body></html>"""


def _pagina(error: str = "", siguiente: str = "") -> str:
    nxt = f"?next={siguiente}" if siguiente in _NEXT_PERMITIDOS and siguiente else ""
    return _PAGINA.replace("__ERROR__", html.escape(error)).replace("__NEXT__", nxt)


def _cookie(resp: Response, request: Request, valor: str, vida: int):
    resp.set_cookie(seg.COOKIE, valor, max_age=vida, httponly=True,
                    samesite="lax", secure=seg.es_https(request), path="/")


def _es_api(path: str) -> bool:
    return path.startswith("/api/")


def instalar(app: FastAPI):
    @app.middleware("http")
    async def _acceso(request: Request, call_next):
        path = request.url.path

        # los ids de las rutas terminan en nombres de archivo de datos/: ni
        # separadores de carpeta (\ en Windows) ni ".." ni caracteres de control
        if "\\" in path or ".." in path.split("/") or any(ord(c) < 32 for c in path):
            return JSONResponse(status_code=400, content={"error": "Ruta inválida."})

        if path == "/health":
            return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

        if not seg.clave_app():
            # Sin contraseña no se abre: solo la propia máquina, directo.
            if seg.es_local(request):
                return await call_next(request)
            return JSONResponse(status_code=503, content={
                "error": "Acceso remoto deshabilitado: falta configurar APP_PASSWORD "
                         "en el servidor."}, headers={"Cache-Control": "no-store"})

        if path == "/login":
            return await call_next(request)

        sesion = seg.leer_sesion(request.cookies.get(seg.COOKIE, ""))
        if not sesion:
            if _es_api(path) or request.method not in _METODOS_SEGUROS:
                return JSONResponse(status_code=401, content={
                    "error": "Sesión vencida o no iniciada.", "login": True},
                    headers={"Cache-Control": "no-store"})
            siguiente = "?next=diario" if path == "/diario" else ""
            return RedirectResponse("login" + siguiente, status_code=303,
                                    headers={"Cache-Control": "no-store"})

        if request.method not in _METODOS_SEGUROS and not seg.origen_valido(request):
            return JSONResponse(status_code=403, content={
                "error": "Origen no permitido."})

        resp = await call_next(request)
        # sesión deslizante: se renueva si pasó más de una hora desde la emisión
        ahora = time.time()
        if sesion["exp"] - ahora < seg.sesion_horas() * 3600 - 3600:
            nueva = seg.emitir_sesion(ahora, inicio=float(sesion["iat"]))
            vida = int(seg.leer_sesion(nueva)["exp"] - ahora)
            _cookie(resp, request, nueva, vida)
        return resp

    @app.get("/login", include_in_schema=False)
    def login_pagina(next: str = ""):
        return HTMLResponse(_pagina(siguiente=next),
                            headers={"Cache-Control": "no-store"})

    @app.post("/login", include_in_schema=False)
    async def login_enviar(request: Request, next: str = ""):
        ip = seg.ip_cliente(request)
        espera = seg.LIMITE_LOGIN.bloqueado(ip)
        quiere_json = "application/json" in request.headers.get("content-type", "")
        if espera:
            msj = f"Demasiados intentos. Probá de nuevo en {espera // 60 + 1} minuto(s)."
            if quiere_json:
                return JSONResponse(status_code=429, content={"error": msj},
                                    headers={"Retry-After": str(espera)})
            return HTMLResponse(_pagina(msj, next), status_code=429,
                                headers={"Retry-After": str(espera)})
        if not seg.origen_valido(request):
            return JSONResponse(status_code=403, content={"error": "Origen no permitido."})
        cuerpo = await request.body()
        try:
            if quiere_json:
                clave = str((json.loads(cuerpo or b"{}") or {}).get("clave", ""))
            else:
                clave = (parse_qs(cuerpo.decode("utf-8", "replace")).get("clave") or [""])[0]
        except (ValueError, AttributeError):
            clave = ""
        if not seg.clave_correcta(clave):
            seg.LIMITE_LOGIN.fallo(ip)
            print(f"[acceso] Login fallido desde {ip}", flush=True)
            if quiere_json:
                return JSONResponse(status_code=401, content={"error": "Contraseña incorrecta."})
            return HTMLResponse(_pagina("Contraseña incorrecta.", next), status_code=401)
        seg.LIMITE_LOGIN.ok(ip)
        destino = _NEXT_PERMITIDOS.get(next, "./")
        resp = (JSONResponse({"ok": True, "destino": destino}) if quiere_json
                else RedirectResponse(destino, status_code=303))
        valor = seg.emitir_sesion()
        _cookie(resp, request, valor, int(seg.leer_sesion(valor)["exp"] - time.time()))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/logout", include_in_schema=False)
    def logout(request: Request):
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(seg.COOKIE, path="/")
        return resp
