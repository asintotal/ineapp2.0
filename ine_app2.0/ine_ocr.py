# OCR de credenciales INE con Tesseract.
# Basado en extraer_ine.py, con varias pasadas de lectura (distinto preprocesado y
# modo de segmentación) que se detienen en cuanto se obtienen los tres datos.
import os
import re
import sys
from collections import Counter

import pytesseract
from PIL import Image, ImageChops, ImageFilter, ImageOps

IDIOMA_OCR = os.environ.get("OCR_LANG", "spa")      # "eng" si no hay español instalado
MAX_ANCHO = int(os.environ.get("OCR_MAX_ANCHO", "2000"))
MIN_ANCHO = 1600
TIMEOUT = int(os.environ.get("OCR_TIMEOUT", "45"))   # segundos por pasada
DEBUG = os.environ.get("OCR_DEBUG", "0") == "1"      # ¡escribe datos personales en los logs!

A_LETRA = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G"}
A_NUMERO = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1",
            "Z": "2", "S": "5", "B": "8", "G": "6"}
REGEX_CURP = re.compile(r"[A-Z]{4}\d{6}[HMX][A-Z]{2}[B-DF-HJ-NP-TV-Z]{3}[A-Z0-9]\d")


# ------------------------------ Preprocesado ---------------------------------
def _gris(img: Image.Image) -> Image.Image:
    """Escala de grises, orientación EXIF y tamaño de trabajo (ni enorme ni diminuto)."""
    img = ImageOps.grayscale(ImageOps.exif_transpose(img))
    if img.width > MAX_ANCHO:
        f = MAX_ANCHO / img.width
        img = img.resize((int(img.width * f), int(img.height * f)), Image.LANCZOS)
    elif img.width < MIN_ANCHO:
        f = MIN_ANCHO / img.width
        img = img.resize((int(img.width * f), int(img.height * f)), Image.LANCZOS)
    return img


def _v_basica(g: Image.Image) -> Image.Image:
    return ImageOps.autocontrast(g)


def _v_sin_fondo(g: Image.Image) -> Image.Image:
    """Quita el fondo con patrones/hologramas: deja solo lo más oscuro que su entorno."""
    fondo = g.filter(ImageFilter.GaussianBlur(max(15, g.width // 60)))
    texto = ImageChops.subtract(fondo, g)          # > 0 donde el píxel es más oscuro que el fondo local
    return ImageOps.autocontrast(ImageOps.invert(texto), cutoff=1)


VARIANTES = {"basica": _v_basica, "sin_fondo": _v_sin_fondo}
# (variante, modo de segmentación de Tesseract)
PASADAS = [("basica", 6), ("sin_fondo", 6), ("sin_fondo", 4), ("basica", 11)]


def _ocr(img: Image.Image, psm: int) -> str:
    try:
        return pytesseract.image_to_string(img, lang=IDIOMA_OCR, config=f"--psm {psm}", timeout=TIMEOUT)
    except pytesseract.TesseractNotFoundError:
        raise RuntimeError("Tesseract no está instalado en el servidor.")
    except pytesseract.TesseractError as e:
        raise RuntimeError(f"Falló el OCR (¿está instalado el idioma '{IDIOMA_OCR}'?): {e}")


# -------------------------- Limpieza / correcciones --------------------------
def corregir_curp(c: str) -> str:
    """Corrige confusiones típicas del OCR según la posición del carácter."""
    if len(c) != 18:
        return c
    v = list(c)
    for i in range(18):
        pos = i + 1
        if pos <= 4 or 11 <= pos <= 16:
            v[i] = A_LETRA.get(v[i], v[i])
        elif 5 <= pos <= 10 or pos == 18:
            v[i] = A_NUMERO.get(v[i], v[i])
    return "".join(v)


_CURP_AISLADO = re.compile(r"(?<![A-Z0-9])" + REGEX_CURP.pattern + r"(?![A-Z0-9])")


def buscar_curp(texto: str) -> tuple[str, bool]:
    """Devuelve (curp, confiable). Confiable = apareció como una palabra completa de 18 caracteres."""
    m = _CURP_AISLADO.search(texto)
    if m:
        return m.group(0), True
    # Ventana de 18 caracteres sobre el texto sin espacios ni símbolos (el OCR a veces parte
    # el CURP o añade/quita un carácter): sirve de candidato, pero no es confiable por sí sola.
    compacto = re.sub(r"[^A-Z0-9]", "", texto)
    for i in range(len(compacto) - 17):
        c = corregir_curp(compacto[i:i + 18])
        if REGEX_CURP.fullmatch(c):
            return c, False
    m = re.search(r"CURP[^A-Z0-9]{0,4}([A-Z0-9]{18})", texto)
    return (corregir_curp(m.group(1)), False) if m else ("", False)


def extraer_curp(texto: str) -> str:
    return buscar_curp(texto)[0]


_DIGITOS = str.maketrans("OILSB", "01158")


def extraer_vigencia(texto: str) -> str:
    """Último año (20xx) junto a la etiqueta VIGENCIA, tolerando errores de lectura."""
    for m in re.finditer(r"V[I1L]GEN\w{1,3}", texto):
        trozo = texto[m.end():m.end() + 25].translate(_DIGITOS)
        seguidos = re.match(r"[^\d]{0,6}((?:20\d{2}[\s\-]*)+)", trozo)   # "2020 2030" -> toma el último
        if seguidos:
            return re.findall(r"20\d{2}", seguidos.group(1))[-1]
    return ""


_ETIQ_NOMBRE = re.compile(r"^\W*N[O0]MBR[EF]\b")
_CORTE = re.compile(r"DOMICILIO|CALLE|COL\.?\s|FRACC|CLAVE|CURP|SEXO|FECHA|NACIMIENTO|EDAD|\d{3}")


def extraer_nombre(lineas: list[str]) -> str:
    """Bajo NOMBRE: apellido paterno, materno y nombre(s). Devuelve 'NOMBRE(S) PATERNO MATERNO'."""
    idx = next((i for i, l in enumerate(lineas) if _ETIQ_NOMBRE.match(l)), None)
    if idx is None:
        return ""
    candidatas: list[str] = []
    resto = _ETIQ_NOMBRE.sub("", lineas[idx]).strip(" :-")
    resto = re.sub(r"[^A-ZÁÉÍÓÚÜÑ ]", "", resto).strip()
    if len(resto) >= 2:
        candidatas.append(resto)
    for l in lineas[idx + 1:]:
        if _CORTE.search(l):
            break
        limpia = re.sub(r"[^A-ZÁÉÍÓÚÜÑ ]", "", l).strip()
        limpia = " ".join(p for p in limpia.split() if len(p) > 1 or p in {"Y", "E"})  # quita letras sueltas de ruido
        if len(limpia) >= 2:
            candidatas.append(limpia)
        if len(candidatas) == 3:
            break
    if not candidatas:
        return ""
    if len(candidatas) >= 3:
        paterno, materno, nombres = candidatas[0], candidatas[1], " ".join(candidatas[2:])
        return " ".join(f"{nombres} {paterno} {materno}".split())
    return " ".join(" ".join(candidatas).split())


# ------------------------ Extracción de todos los datos ----------------------
def _campos(texto: str) -> dict:
    texto = texto.upper()
    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    curp, confiable = buscar_curp(texto)
    return {"nombre": extraer_nombre(lineas), "curp": curp, "curp_confiable": confiable,
            "vigencia": extraer_vigencia(texto)}


def _elegir_curp(votos: Counter, confiables: set, dudoso: str) -> str:
    """Prefiere CURP confiables; entre candidatos, el que más pasadas repiten."""
    for grupo in (confiables, set(votos)):
        validos = [(votos[c], c) for c in grupo if REGEX_CURP.fullmatch(c)]
        if validos:
            return max(validos)[1]
    return dudoso


def extraer_datos(img: Image.Image) -> dict:
    """Prueba varias pasadas; se detiene al tener nombre, vigencia y un CURP confiable."""
    gris = _gris(img)
    cache: dict[str, Image.Image] = {}
    final = {"nombre": "", "vigencia": ""}
    votos: Counter = Counter()
    confiables: set = set()
    dudoso = ""
    errores = 0
    for i, (variante, psm) in enumerate(PASADAS, 1):
        if variante not in cache:
            cache[variante] = VARIANTES[variante](gris)
        try:
            texto = _ocr(cache[variante], psm)
        except RuntimeError as e:
            if "timeout" in str(e).lower():
                errores += 1
                continue
            raise
        d = _campos(texto)
        if DEBUG:
            print(f"[ine_ocr] pasada {i} ({variante}, psm {psm}): {d}\n---\n{texto}\n---", file=sys.stderr, flush=True)
        if d["nombre"] and not final["nombre"]:
            final["nombre"] = d["nombre"]
        if d["vigencia"] and not final["vigencia"]:
            final["vigencia"] = d["vigencia"]
        if d["curp"]:
            if REGEX_CURP.fullmatch(d["curp"]):
                votos[d["curp"]] += 1
                if d["curp_confiable"]:
                    confiables.add(d["curp"])
            elif not dudoso:
                dudoso = d["curp"]
        if final["nombre"] and final["vigencia"] and confiables:
            break
    if errores == len(PASADAS):
        raise RuntimeError("Tesseract tardó demasiado en todas las pasadas.")
    final["curp"] = _elegir_curp(votos, confiables, dudoso)
    return final
