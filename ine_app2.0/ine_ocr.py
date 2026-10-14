# OCR del CURP de credenciales INE con Tesseract.
# Solo se lee el CURP (no el nombre ni la vigencia): varias pasadas de lectura
# (distinto preprocesado y modo de segmentación) que se detienen en cuanto aparece
# un CURP confiable.
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
MAX_PASADAS = int(os.environ.get("OCR_MAX_PASADAS", "4"))  # menos pasadas = más rápido, pero lee peor las fotos difíciles
DEBUG = os.environ.get("OCR_DEBUG", "0") == "1"      # ¡escribe datos personales en los logs!

A_LETRA = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G"}
A_NUMERO = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1",
            "Z": "2", "S": "5", "B": "8", "G": "6"}
REGEX_CURP = re.compile(r"[A-Z]{4}\d{6}[HMX][A-Z]{2}[B-DF-HJ-NP-TV-Z]{3}[A-Z0-9]\d")
_CURP_AISLADO = re.compile(r"(?<![A-Z0-9])" + REGEX_CURP.pattern + r"(?![A-Z0-9])")


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


def buscar_curp(texto: str) -> tuple[str, bool]:
    """Devuelve (curp, confiable). Confiable = apareció como una palabra completa de 18 caracteres."""
    texto = texto.upper()
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


def _elegir(votos: Counter, confiables: set, dudoso: str) -> str:
    """Prefiere CURP confiables; entre candidatos, el que más pasadas repiten."""
    for grupo in (confiables, set(votos)):
        validos = [(votos[c], c) for c in grupo if REGEX_CURP.fullmatch(c)]
        if validos:
            return max(validos)[1]
    return dudoso


def extraer_datos(img: Image.Image) -> dict:
    """Lee el CURP. Devuelve {"curp": str, "pasadas": int}; curp vacío si no se encontró."""
    gris = _gris(img)
    cache: dict[str, Image.Image] = {}
    votos: Counter = Counter()
    confiables: set = set()
    dudoso = ""
    errores = 0
    pasadas = PASADAS[:max(1, MAX_PASADAS)]
    hechas = 0
    for i, (variante, psm) in enumerate(pasadas, 1):
        if variante not in cache:
            cache[variante] = VARIANTES[variante](gris)
        try:
            texto = _ocr(cache[variante], psm)
        except RuntimeError as e:
            if "timeout" in str(e).lower():
                errores += 1
                continue
            raise
        hechas = i
        curp, confiable = buscar_curp(texto)
        if DEBUG:
            print(f"[ine_ocr] pasada {i} ({variante}, psm {psm}): curp={curp!r} confiable={confiable}\n---\n{texto}\n---",
                  file=sys.stderr, flush=True)
        if curp:
            if REGEX_CURP.fullmatch(curp):
                votos[curp] += 1
                if confiable:
                    confiables.add(curp)
            elif not dudoso:
                dudoso = curp
        if confiables:          # solo se detiene con un CURP confiable (palabra completa de 18 caracteres)
            break
    if errores == len(pasadas):
        raise RuntimeError("Tesseract tardó demasiado en todas las pasadas.")
    return {"curp": _elegir(votos, confiables, dudoso), "pasadas": hechas}
