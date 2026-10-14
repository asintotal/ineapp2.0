#!/usr/bin/env python3
"""Aplicación web para capturar el CURP de credenciales INE y dar seguimiento a la CSF.

Roles:
  captura   -> toma la foto de la credencial, ve el CURP leído y guarda el registro.
  operacion -> ve la tabla diaria de registros (n.º, fecha y hora, CURP, imagen) y marca "CSF Generado".
  admin     -> accede a ambas pantallas, puede eliminar registros y descargar el CSV.

Las imágenes se guardan cifradas (Fernet) en disco; la base de datos es SQLite.
"""
import csv
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import click
from cryptography.fernet import Fernet, InvalidToken
from flask import (Flask, Response, abort, g, jsonify, redirect, render_template,
                   request, session, url_for)
from PIL import Image, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash

import ine_ocr

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DIR_IMAGENES = DATA_DIR / "credenciales"
DIR_BORRADORES = DATA_DIR / "borradores"
RUTA_BD = DATA_DIR / "consultas_ine.db"
MAX_MB = int(os.environ.get("MAX_MB", "10"))
FORMATOS = {"JPEG": ".jpg", "PNG": ".png", "BMP": ".bmp", "TIFF": ".tif", "WEBP": ".webp"}
ROLES = ("captura", "operacion", "admin")
ROL_NOMBRE = {"captura": "Captura", "operacion": "Operación", "admin": "Administración"}
INICIO = {"captura": "captura", "operacion": "operacion", "admin": "operacion"}
HORAS_BORRADOR = 2  # un borrador (foto leída pero no guardada) caduca a las 2 horas

try:
    from zoneinfo import ZoneInfo
    ZONA = ZoneInfo(os.environ.get("ZONA_HORARIA", "America/Mexico_City"))
except Exception:  # sin base de datos de zonas horarias: México (centro) sin horario de verano
    ZONA = timezone(timedelta(hours=-6))


def ahora() -> datetime:
    return datetime.now(ZONA)


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", ""),
    MAX_CONTENT_LENGTH=MAX_MB * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=int(os.environ.get("SESION_HORAS", "8"))),
)
app.logger.setLevel(logging.INFO)
if not app.config["SECRET_KEY"]:
    raise SystemExit("Falta SECRET_KEY en el entorno (ver .env.example).")
_fk = os.environ.get("FERNET_KEY", "")
if not _fk:
    raise SystemExit("Falta FERNET_KEY en el entorno. Genérala con: python -c "
                     "\"import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())\"")
FERNET = Fernet(_fk.encode())


# ----------------------------------- CSRF ------------------------------------
def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def verificar_csrf():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        enviado = request.headers.get("X-CSRFToken") or request.form.get("csrf_token", "")
        esperado = session.get("csrf", "")
        if not esperado or not hmac.compare_digest(enviado, esperado):
            if request.path.startswith("/api/"):
                return jsonify(error="Token de seguridad inválido. Recarga la página."), 400
            abort(400)


Image.MAX_IMAGE_PIXELS = 60_000_000  # protección contra "bombas de descompresión"


# ------------------------------- Base de datos -------------------------------
ESQUEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario TEXT UNIQUE NOT NULL,
    hash TEXT NOT NULL,
    es_admin INTEGER NOT NULL DEFAULT 0,
    rol TEXT NOT NULL DEFAULT 'captura'
);
CREATE TABLE IF NOT EXISTS consultas (
    numero_consulta INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha_consulta  TEXT NOT NULL,
    hora_consulta   TEXT NOT NULL,
    curp            TEXT,
    usuario         TEXT NOT NULL,
    imagen          TEXT,
    csf_generado    INTEGER NOT NULL DEFAULT 0,
    csf_fecha       TEXT,
    csf_usuario     TEXT
);
CREATE TABLE IF NOT EXISTS borradores (
    token   TEXT PRIMARY KEY,
    usuario TEXT NOT NULL,
    curp    TEXT NOT NULL,
    imagen  TEXT NOT NULL,
    creado  TEXT NOT NULL
);
"""
_bd_lista = False


def _columnas(con, tabla):
    return {r["name"] for r in con.execute(f"PRAGMA table_info({tabla})")}


def preparar_bd(con: sqlite3.Connection) -> None:
    """Crea las tablas y migra bases creadas con versiones anteriores (sin perder datos)."""
    con.executescript(ESQUEMA)
    with con:
        if "rol" not in _columnas(con, "usuarios"):
            con.execute("ALTER TABLE usuarios ADD COLUMN rol TEXT")
        con.execute("UPDATE usuarios SET rol = CASE WHEN es_admin = 1 THEN 'admin' ELSE 'captura' END "
                    "WHERE rol IS NULL OR rol = ''")
        cols = _columnas(con, "consultas")
        if "csf_generado" not in cols:
            con.execute("ALTER TABLE consultas ADD COLUMN csf_generado INTEGER NOT NULL DEFAULT 0")
        if "csf_fecha" not in cols:
            con.execute("ALTER TABLE consultas ADD COLUMN csf_fecha TEXT")
        if "csf_usuario" not in cols:
            con.execute("ALTER TABLE consultas ADD COLUMN csf_usuario TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS idx_consultas_fecha ON consultas (fecha_consulta)")


def db() -> sqlite3.Connection:
    global _bd_lista
    if "db" not in g:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(RUTA_BD, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        if not _bd_lista:
            preparar_bd(g.db)
            _bd_lista = True
    return g.db


@app.teardown_appcontext
def cerrar_bd(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


# ------------------------------- Autenticación -------------------------------
_fallos: dict[str, list[float]] = {}  # limitador simple en memoria (1 proceso)
MAX_FALLOS, VENTANA = 5, 300


def bloqueado(clave: str) -> bool:
    t = time.time()
    _fallos[clave] = [x for x in _fallos.get(clave, []) if t - x < VENTANA]
    return len(_fallos[clave]) >= MAX_FALLOS


def acceso(*roles):
    """Exige sesión y que el rol del usuario esté en `roles` (admin entra a todo)."""
    def deco(f):
        @wraps(f)
        def w(*a, **kw):
            if "usuario" not in session:
                if request.path.startswith("/api/"):
                    return jsonify(error="Sesión expirada. Vuelve a iniciar sesión."), 401
                return redirect(url_for("login"))
            rol = session.get("rol")
            if roles and rol != "admin" and rol not in roles:
                abort(403)
            return f(*a, **kw)
        return w
    return deco


@app.errorhandler(403)
def prohibido(_e):
    if request.path.startswith("/api/"):
        return jsonify(error="No tienes permiso para esta acción."), 403
    return render_template("prohibido.html", home=url_for("inicio") if "usuario" in session else url_for("login")), 403


@app.errorhandler(413)
def demasiado_grande(_e):
    if request.path.startswith("/api/"):
        return jsonify(error=f"La imagen es demasiado grande (máximo {MAX_MB} MB)."), 413
    return "Archivo demasiado grande", 413


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        clave = f"{request.remote_addr}|{usuario.lower()}"
        if bloqueado(clave):
            error = "Demasiados intentos fallidos. Espera unos minutos."
        else:
            u = db().execute("SELECT * FROM usuarios WHERE usuario=?", (usuario,)).fetchone()
            if u and check_password_hash(u["hash"], request.form.get("clave", "")):
                session.clear()
                session.permanent = True
                session["usuario"], session["rol"] = u["usuario"], u["rol"] if u["rol"] in ROLES else "captura"
                return redirect(url_for("inicio"))
            _fallos.setdefault(clave, []).append(time.time())
            error = "Usuario o contraseña incorrectos."
    return render_template("login.html", error=error)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@acceso()
def inicio():
    return redirect(url_for(INICIO[session["rol"]]))


def contexto():
    return dict(usuario=session["usuario"], rol=session["rol"], rol_nombre=ROL_NOMBRE[session["rol"]])


@app.get("/captura")
@acceso("captura")
def captura():
    return render_template("captura.html", **contexto())


@app.get("/operacion")
@acceso("operacion")
def operacion():
    return render_template("operacion.html", hoy=ahora().strftime("%Y-%m-%d"), **contexto())


# ----------------------------- Captura (rol captura) -------------------------
def abrir_imagen(datos: bytes):
    """Valida que sea una imagen real y devuelve (imagen, extensión)."""
    try:
        with Image.open(io.BytesIO(datos)) as im:
            formato = im.format
            if formato == "JPEG":
                im.draft("RGB", (3000, 3000))  # decodifica a menor tamaño: mucha menos memoria con fotos enormes
            im.load()
            img = im.copy()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("El archivo no es una imagen válida.")
    if formato not in FORMATOS:
        raise ValueError("Formato no soportado. Usa JPG, PNG, BMP, TIFF o WEBP.")
    return img, FORMATOS[formato]


def limpiar_borradores(con) -> None:
    limite = (ahora() - timedelta(hours=HORAS_BORRADOR)).isoformat(timespec="seconds")
    for r in con.execute("SELECT token, imagen FROM borradores WHERE creado < ?", (limite,)).fetchall():
        (DIR_BORRADORES / Path(r["imagen"]).name).unlink(missing_ok=True)
    with con:
        con.execute("DELETE FROM borradores WHERE creado < ?", (limite,))


@app.post("/api/leer")
@acceso("captura")
def api_leer():
    """Lee el CURP de la foto. No guarda ningún registro: solo deja un borrador cifrado."""
    f = request.files.get("imagen")
    if not f:
        return jsonify(error="No se recibió ninguna imagen."), 400
    datos_img = f.read()
    try:
        img, ext = abrir_imagen(datos_img)
    except ValueError as e:
        return jsonify(error=str(e)), 400

    t0 = time.perf_counter()
    try:
        datos = ine_ocr.extraer_datos(img)
    except RuntimeError as e:
        app.logger.error("OCR: %s", e)
        return jsonify(error="No se pudo procesar la imagen en el servidor."), 500
    # Sin datos personales: solo tiempos, para saber cuánto tarda el servidor en realidad
    app.logger.info("OCR: %.1f s, %s pasada(s), imagen %dx%d", time.perf_counter() - t0, datos["pasadas"], *img.size)

    curp = datos["curp"]
    valido = bool(ine_ocr.REGEX_CURP.fullmatch(curp))
    resp = {"curp": curp, "curp_valido": valido}
    if valido:  # solo se puede guardar un CURP con la estructura oficial
        con = db()
        limpiar_borradores(con)
        token = secrets.token_urlsafe(24)
        archivo = f"{token}{ext}.enc"
        DIR_BORRADORES.mkdir(parents=True, exist_ok=True)
        destino = DIR_BORRADORES / archivo
        destino.write_bytes(FERNET.encrypt(datos_img))
        os.chmod(destino, 0o600)
        with con:
            con.execute("INSERT INTO borradores (token,usuario,curp,imagen,creado) VALUES (?,?,?,?,?)",
                        (token, session["usuario"], curp, archivo, ahora().isoformat(timespec="seconds")))
        resp["token"] = token
    return jsonify(resp)


def _token_valido(token) -> bool:
    return isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token) is not None


@app.post("/api/guardar")
@acceso("captura")
def api_guardar():
    """Convierte el borrador leído en un registro definitivo (el CURP lo pone el servidor)."""
    token = (request.get_json(silent=True) or {}).get("token", "")
    con = db()
    b = con.execute("SELECT * FROM borradores WHERE token=? AND usuario=?",
                    (token, session["usuario"])).fetchone() if _token_valido(token) else None
    if not b:
        return jsonify(error="La captura expiró o ya se guardó. Toma la foto de nuevo."), 404
    t = ahora()
    fecha, hora = t.strftime("%Y-%m-%d"), t.strftime("%H:%M:%S")
    origen = DIR_BORRADORES / Path(b["imagen"]).name
    ext = Path(Path(b["imagen"]).stem).suffix            # "TOKEN.jpg.enc" -> ".jpg"
    destino = None
    try:
        with con:  # commit si todo sale bien; rollback si algo falla
            cur = con.execute(
                "INSERT INTO consultas (fecha_consulta,hora_consulta,curp,usuario) VALUES (?,?,?,?)",
                (fecha, hora, b["curp"], session["usuario"]))
            numero = cur.lastrowid
            nombre_arch = f"ID_{numero}_{fecha}{ext}.enc"
            DIR_IMAGENES.mkdir(parents=True, exist_ok=True)
            destino = DIR_IMAGENES / nombre_arch
            os.replace(origen, destino)
            con.execute("UPDATE consultas SET imagen=? WHERE numero_consulta=?", (nombre_arch, numero))
            con.execute("DELETE FROM borradores WHERE token=?", (token,))
    except Exception:
        if destino and destino.exists() and not origen.exists():
            os.replace(destino, origen)
        app.logger.exception("Error al guardar la consulta")
        return jsonify(error="No se pudo guardar el registro. Inténtalo de nuevo."), 500
    return jsonify(numero_consulta=numero, fecha=fecha, hora=hora, curp=b["curp"])


@app.post("/api/descartar")
@acceso("captura")
def api_descartar():
    token = (request.get_json(silent=True) or {}).get("token", "")
    if _token_valido(token):
        con = db()
        b = con.execute("SELECT imagen FROM borradores WHERE token=? AND usuario=?",
                        (token, session["usuario"])).fetchone()
        if b:
            (DIR_BORRADORES / Path(b["imagen"]).name).unlink(missing_ok=True)
            with con:
                con.execute("DELETE FROM borradores WHERE token=?", (token,))
    return jsonify(ok=True)


# --------------------------- Operación (rol operacion) -----------------------
@app.get("/api/registros")
@acceso("operacion")
def api_registros():
    fecha = request.args.get("fecha") or ahora().strftime("%Y-%m-%d")
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        return jsonify(error="Fecha inválida."), 400
    filas = db().execute(
        "SELECT numero_consulta, fecha_consulta, hora_consulta, curp, "
        "       (imagen IS NOT NULL AND imagen <> '') AS tiene_imagen, csf_generado "
        "FROM consultas WHERE fecha_consulta=? ORDER BY numero_consulta DESC", (fecha,)).fetchall()
    return jsonify(fecha=fecha, registros=[
        {"numero": r["numero_consulta"], "fecha_hora": f"{r['fecha_consulta']} {r['hora_consulta']}",
         "curp": r["curp"] or "", "imagen": bool(r["tiene_imagen"]), "csf": bool(r["csf_generado"])}
        for r in filas])


@app.post("/api/registros/<int:numero>/csf")
@acceso("operacion")
def api_csf(numero):
    """Marca el registro como "CSF Generado" (una sola vez; no se puede deshacer)."""
    con = db()
    t = ahora().isoformat(timespec="seconds")
    with con:
        con.execute("UPDATE consultas SET csf_generado=1, csf_fecha=?, csf_usuario=? "
                    "WHERE numero_consulta=? AND csf_generado=0", (t, session["usuario"], numero))
    if not con.execute("SELECT 1 FROM consultas WHERE numero_consulta=?", (numero,)).fetchone():
        return jsonify(error="El registro no existe."), 404
    return jsonify(ok=True)


@app.get("/imagen/<int:numero>")
@acceso("operacion")
def imagen(numero):
    r = db().execute("SELECT imagen FROM consultas WHERE numero_consulta=?", (numero,)).fetchone()
    if not r or not r["imagen"]:
        abort(404)
    ruta = DIR_IMAGENES / Path(r["imagen"]).name  # evita rutas fuera de la carpeta
    if not ruta.is_file():
        abort(404)
    try:
        contenido = FERNET.decrypt(ruta.read_bytes())
    except InvalidToken:
        abort(500)
    ext = Path(Path(r["imagen"]).stem).suffix.lower()  # "ID_7_2026-10-12.jpg.enc" -> ".jpg"
    mime = {".jpg": "image/jpeg", ".png": "image/png", ".bmp": "image/bmp",
            ".tif": "image/tiff", ".webp": "image/webp"}.get(ext, "application/octet-stream")
    return Response(contenido, mimetype=mime)


# --------------------------------- Administración ----------------------------
def borrar_consulta(con, numero):
    r = con.execute("SELECT imagen FROM consultas WHERE numero_consulta=?", (numero,)).fetchone()
    if r and r["imagen"]:
        (DIR_IMAGENES / Path(r["imagen"]).name).unlink(missing_ok=True)
    with con:
        con.execute("DELETE FROM consultas WHERE numero_consulta=?", (numero,))


@app.delete("/api/registros/<int:numero>")
@acceso()
def api_borrar(numero):
    if session.get("rol") != "admin":
        abort(403)
    borrar_consulta(db(), numero)
    return jsonify(ok=True)


@app.get("/csv")
@acceso()
def exportar_csv():
    if session.get("rol") != "admin":
        abort(403)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["numero_consulta", "fecha_consulta", "hora_consulta", "curp", "usuario_captura",
                "csf_generado", "csf_fecha", "csf_usuario"])
    for r in db().execute("SELECT * FROM consultas ORDER BY numero_consulta"):
        w.writerow([r["numero_consulta"], r["fecha_consulta"], r["hora_consulta"], r["curp"], r["usuario"],
                    "SI" if r["csf_generado"] else "NO", r["csf_fecha"] or "", r["csf_usuario"] or ""])
    return Response("﻿" + out.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=consultas_ine.csv"})


@app.after_request
def cabeceras(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'")
    if request.endpoint != "static":
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ----------------------------- Alta de usuarios ------------------------------
def guardar_usuario(con, usuario: str, clave: str, rol: str) -> bool:
    """Crea o actualiza un usuario. Devuelve True si hubo algún cambio."""
    fila = con.execute("SELECT hash, rol FROM usuarios WHERE usuario=?", (usuario,)).fetchone()
    if fila and fila["rol"] == rol and check_password_hash(fila["hash"], clave):
        return False
    with con:
        con.execute(
            "INSERT INTO usuarios (usuario,hash,es_admin,rol) VALUES (?,?,?,?) "
            "ON CONFLICT(usuario) DO UPDATE SET hash=excluded.hash, es_admin=excluded.es_admin, rol=excluded.rol",
            (usuario, generate_password_hash(clave), int(rol == "admin"), rol))
    return True


def sembrar_usuarios() -> None:
    """Crea/actualiza usuarios al arrancar a partir de variables de entorno.

    ADMIN_USUARIO + ADMIN_CLAVE   -> rol admin
    USUARIOS (JSON, opcional)     -> [{"usuario":"ana","clave":"...","rol":"captura"},
                                      {"usuario":"luis","clave":"...","rol":"operacion"}]
    (por compatibilidad, {"admin": true} equivale a "rol": "admin"; sin rol se asume "captura")
    """
    pedidos = []
    if os.environ.get("ADMIN_USUARIO") and os.environ.get("ADMIN_CLAVE"):
        pedidos.append({"usuario": os.environ["ADMIN_USUARIO"].strip(),
                        "clave": os.environ["ADMIN_CLAVE"], "rol": "admin"})
    extra = os.environ.get("USUARIOS", "").strip()
    if extra:
        try:
            pedidos += list(json.loads(extra))
        except (ValueError, TypeError):
            app.logger.error("USUARIOS no es un JSON válido; se ignora.")
    if not pedidos:
        return
    with app.app_context():
        con = db()
        for p in pedidos:
            try:
                usuario, clave = str(p["usuario"]).strip(), str(p["clave"])
                rol = str(p.get("rol") or ("admin" if p.get("admin") else "captura")).strip().lower()
            except (KeyError, TypeError, AttributeError):
                app.logger.error("Entrada de usuario inválida en USUARIOS; se omite.")
                continue
            if rol not in ROLES:
                app.logger.error("Usuario '%s' omitido: rol '%s' no válido (usa captura, operacion o admin).", usuario, rol)
                continue
            if not usuario or len(clave) < 10:
                app.logger.error("Usuario '%s' omitido: nombre vacío o contraseña de menos de 10 caracteres.", usuario)
                continue
            if guardar_usuario(con, usuario, clave, rol):
                app.logger.info("Usuario '%s' (%s) creado/actualizado desde variables de entorno.", usuario, rol)


sembrar_usuarios()


# ------------------------------------ CLI ------------------------------------
@app.cli.command("crear-usuario")
@click.argument("usuario")
@click.option("--rol", type=click.Choice(ROLES), default="captura", show_default=True)
@click.option("--admin", is_flag=True, help="Atajo de --rol admin.")
@click.password_option()
def crear_usuario(usuario, rol, admin, password):
    """Crea o actualiza un usuario."""
    if len(password) < 10:
        raise click.ClickException("La contraseña debe tener al menos 10 caracteres.")
    rol = "admin" if admin else rol
    with app.app_context():
        guardar_usuario(db(), usuario, password, rol)
    click.echo(f"Usuario '{usuario}' guardado con rol {rol}.")


@app.cli.command("purgar")
@click.option("--dias", default=0, help="Borra consultas más antiguas que N días.")
def purgar(dias):
    """Elimina consultas e imágenes con más de N días (política de retención)."""
    dias = dias or int(os.environ.get("RETENCION_DIAS", "0"))
    if dias <= 0:
        raise click.ClickException("Indica --dias N (>0) o define RETENCION_DIAS.")
    limite = (ahora() - timedelta(days=dias)).strftime("%Y-%m-%d")
    with app.app_context():
        con = db()
        viejas = con.execute("SELECT numero_consulta FROM consultas WHERE fecha_consulta < ?", (limite,)).fetchall()
        for r in viejas:
            borrar_consulta(con, r["numero_consulta"])
    click.echo(f"{len(viejas)} consultas eliminadas (anteriores a {limite}).")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
