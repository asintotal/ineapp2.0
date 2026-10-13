#!/usr/bin/env python3
"""Aplicación web para extraer NOMBRE, CURP y VIGENCIA de credenciales INE.

- Inicio de sesión obligatorio (usuarios creados por el administrador).
- OCR con Tesseract (ine_ocr.py).
- Consultas en SQLite; imágenes cifradas en disco (Fernet) en credenciales/.
- Descarga CSV, borrado (solo admin) y purga por retención.
"""
import csv
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
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
RUTA_BD = DATA_DIR / "consultas_ine.db"
MAX_MB = int(os.environ.get("MAX_MB", "10"))
RETENCION_DIAS = int(os.environ.get("RETENCION_DIAS", "0"))  # 0 = no purgar automáticamente
FORMATOS = {"JPEG": ".jpg", "PNG": ".png", "BMP": ".bmp", "TIFF": ".tif", "WEBP": ".webp"}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", ""),
    MAX_CONTENT_LENGTH=MAX_MB * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=int(os.environ.get("SESION_HORAS", "8"))),
)
if not app.config["SECRET_KEY"]:
    raise SystemExit("Falta SECRET_KEY en el entorno (ver .env.example).")
_fk = os.environ.get("FERNET_KEY", "")
if not _fk:
    raise SystemExit("Falta FERNET_KEY en el entorno. Genérala con: python -c "
                     "\"from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())\"")
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
def db() -> sqlite3.Connection:
    if "db" not in g:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(RUTA_BD, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario TEXT UNIQUE NOT NULL,
                hash TEXT NOT NULL,
                es_admin INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS consultas (
                numero_consulta INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha_consulta  TEXT NOT NULL,
                hora_consulta   TEXT NOT NULL,
                nombre          TEXT,
                curp            TEXT,
                vigencia        TEXT,
                usuario         TEXT NOT NULL,
                imagen          TEXT
            );
            """
        )
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
    ahora = time.time()
    _fallos[clave] = [t for t in _fallos.get(clave, []) if ahora - t < VENTANA]
    return len(_fallos[clave]) >= MAX_FALLOS


def login_requerido(admin: bool = False):
    def deco(f):
        @wraps(f)
        def w(*a, **kw):
            if "usuario" not in session:
                if request.path.startswith("/api/"):
                    return jsonify(error="Sesión expirada. Vuelve a iniciar sesión."), 401
                return redirect(url_for("login"))
            if admin and not session.get("admin"):
                abort(403)
            return f(*a, **kw)
        return w
    return deco


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
                session["usuario"], session["admin"] = u["usuario"], bool(u["es_admin"])
                return redirect(url_for("index"))
            _fallos.setdefault(clave, []).append(time.time())
            error = "Usuario o contraseña incorrectos."
    return render_template("login.html", error=error)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ----------------------------------- Vistas ----------------------------------
@app.get("/")
@login_requerido()
def index():
    return render_template("index.html", usuario=session["usuario"], admin=session.get("admin"))


def filas(limite=500):
    return db().execute(
        "SELECT * FROM consultas ORDER BY numero_consulta DESC LIMIT ?", (limite,)
    ).fetchall()


@app.get("/api/consultas")
@login_requerido()
def api_consultas():
    return jsonify([dict(r) for r in filas()])


@app.post("/api/extraer")
@login_requerido()
def api_extraer():
    f = request.files.get("imagen")
    if not f:
        return jsonify(error="No se recibió ninguna imagen."), 400
    datos_img = f.read()
    try:
        with Image.open(io.BytesIO(datos_img)) as im:
            formato = im.format
            if formato == "JPEG":
                im.draft("RGB", (3000, 3000))  # decodifica a menor tamaño: mucha menos memoria con fotos enormes
            im.load()
            img = im.copy()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return jsonify(error="El archivo no es una imagen válida."), 400
    if formato not in FORMATOS:
        return jsonify(error="Formato no soportado. Usa JPG, PNG, BMP, TIFF o WEBP."), 400
    ext = FORMATOS[formato]

    try:
        datos = ine_ocr.extraer_datos(img)
    except RuntimeError as e:
        app.logger.error("OCR: %s", e)
        return jsonify(error="No se pudo procesar la imagen en el servidor."), 500

    ahora = datetime.now()
    fecha, hora = ahora.strftime("%Y-%m-%d"), ahora.strftime("%H:%M:%S")
    con = db()
    destino = None
    try:
        with con:  # commit si todo sale bien; rollback si algo falla
            cur = con.execute(
                "INSERT INTO consultas (fecha_consulta,hora_consulta,nombre,curp,vigencia,usuario)"
                " VALUES (?,?,?,?,?,?)",
                (fecha, hora, datos["nombre"], datos["curp"], datos["vigencia"], session["usuario"]),
            )
            numero = cur.lastrowid
            nombre_arch = f"ID_{numero}_{fecha}{ext}.enc"
            DIR_IMAGENES.mkdir(parents=True, exist_ok=True)
            destino = DIR_IMAGENES / nombre_arch
            destino.write_bytes(FERNET.encrypt(datos_img))
            os.chmod(destino, 0o600)
            con.execute("UPDATE consultas SET imagen=? WHERE numero_consulta=?", (nombre_arch, numero))
    except Exception:
        if destino and destino.exists():
            destino.unlink()
        app.logger.exception("Error al registrar la consulta")
        return jsonify(error="No se pudo guardar la consulta."), 500

    return jsonify(numero_consulta=numero, fecha=fecha, hora=hora, **datos,
                   curp_valido=bool(ine_ocr.REGEX_CURP.fullmatch(datos["curp"])))


@app.get("/imagen/<int:numero>")
@login_requerido()
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


@app.get("/csv")
@login_requerido()
def exportar_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["numero_consulta", "fecha_consulta", "hora_consulta", "nombre", "curp", "vigencia", "usuario"])
    for r in reversed(filas(100000)):
        w.writerow([r["numero_consulta"], r["fecha_consulta"], r["hora_consulta"],
                    r["nombre"], r["curp"], r["vigencia"], r["usuario"]])
    return Response("﻿" + out.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=consultas_ine.csv"})


def borrar_consulta(con, numero):
    r = con.execute("SELECT imagen FROM consultas WHERE numero_consulta=?", (numero,)).fetchone()
    if r and r["imagen"]:
        (DIR_IMAGENES / Path(r["imagen"]).name).unlink(missing_ok=True)
    with con:
        con.execute("DELETE FROM consultas WHERE numero_consulta=?", (numero,))


@app.delete("/api/consultas/<int:numero>")
@login_requerido(admin=True)
def api_borrar(numero):
    borrar_consulta(db(), numero)
    return jsonify(ok=True)


@app.errorhandler(413)
def demasiado_grande(_e):
    if request.path.startswith("/api/"):
        return jsonify(error=f"La imagen es demasiado grande (máximo {MAX_MB} MB)."), 413
    return "Archivo demasiado grande", 413


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


# ------------------------------------ CLI ------------------------------------
@app.cli.command("crear-usuario")
@click.argument("usuario")
@click.option("--admin", is_flag=True, help="Puede eliminar consultas.")
@click.password_option()
def crear_usuario(usuario, admin, password):
    """Crea o actualiza un usuario."""
    if len(password) < 10:
        raise click.ClickException("La contraseña debe tener al menos 10 caracteres.")
    with app.app_context():
        con = db()
        with con:
            con.execute(
                "INSERT INTO usuarios (usuario,hash,es_admin) VALUES (?,?,?) "
                "ON CONFLICT(usuario) DO UPDATE SET hash=excluded.hash, es_admin=excluded.es_admin",
                (usuario, generate_password_hash(password), int(admin)),
            )
    click.echo(f"Usuario '{usuario}' guardado{' (admin)' if admin else ''}.")


@app.cli.command("purgar")
@click.option("--dias", default=RETENCION_DIAS, show_default=True, help="Borra consultas más antiguas.")
def purgar(dias):
    """Elimina consultas e imágenes con más de N días (política de retención)."""
    if dias <= 0:
        raise click.ClickException("Indica --dias N (>0) o define RETENCION_DIAS.")
    limite = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")
    with app.app_context():
        con = db()
        viejas = con.execute("SELECT numero_consulta FROM consultas WHERE fecha_consulta < ?", (limite,)).fetchall()
        for r in viejas:
            borrar_consulta(con, r["numero_consulta"])
    click.echo(f"{len(viejas)} consultas eliminadas (anteriores a {limite}).")


# ------------------- Usuarios desde variables de entorno ---------------------
def sembrar_usuarios() -> None:
    """Crea/actualiza usuarios al arrancar a partir de variables de entorno.

    ADMIN_USUARIO + ADMIN_CLAVE        -> administrador
    USUARIOS (JSON, opcional)          -> [{"usuario":"maria","clave":"...","admin":false}, ...]
    La contraseña solo se vuelve a guardar si cambió respecto a la almacenada.
    """
    pedidos = []
    if os.environ.get("ADMIN_USUARIO") and os.environ.get("ADMIN_CLAVE"):
        pedidos.append({"usuario": os.environ["ADMIN_USUARIO"].strip(),
                        "clave": os.environ["ADMIN_CLAVE"], "admin": True})
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
                admin = int(bool(p.get("admin", False)))
            except (KeyError, TypeError, AttributeError):
                app.logger.error("Entrada de usuario inválida en USUARIOS; se omite.")
                continue
            if not usuario or len(clave) < 10:
                app.logger.error("Usuario '%s' omitido: nombre vacío o contraseña de menos de 10 caracteres.", usuario)
                continue
            fila = con.execute("SELECT hash, es_admin FROM usuarios WHERE usuario=?", (usuario,)).fetchone()
            if fila and check_password_hash(fila["hash"], clave) and fila["es_admin"] == admin:
                continue  # ya está al día
            with con:
                con.execute(
                    "INSERT INTO usuarios (usuario,hash,es_admin) VALUES (?,?,?) "
                    "ON CONFLICT(usuario) DO UPDATE SET hash=excluded.hash, es_admin=excluded.es_admin",
                    (usuario, generate_password_hash(clave), admin))
            app.logger.info("Usuario '%s' creado/actualizado desde variables de entorno.", usuario)


sembrar_usuarios()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
