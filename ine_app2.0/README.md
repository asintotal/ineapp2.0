# Extractor de datos INE (aplicación web)

Aplicación Flask basada en `extraer_ine.py`: lee **solo el CURP** de la credencial INE y tiene dos roles:

- **Captura**: toma la foto de la credencial, ve el CURP leído (y si tiene la estructura oficial) y lo guarda. No puede editar el CURP (si está mal, toma otra foto) ni ver registros.
- **Operación**: ve la tabla de registros del día (n.º de consulta, fecha y hora `AAAA-MM-DD HH:MM:SS`, CURP e imagen) y, por cada registro, el botón **CSF Generado**: al pulsarlo se deshabilita y la fila cambia de color. La marca es permanente y queda guardado quién la hizo.
- **Admin** (opcional): ve ambas pantallas, elimina registros y descarga el CSV.

Cada consulta queda en SQLite y la imagen se guarda **cifrada** en `credenciales/ID_<número>_<fecha>.<ext>.enc`.

## Qué incluye
- Inicio de sesión obligatorio (usuarios creados por el administrador) y límite de intentos fallidos.
- OCR con Tesseract solo para el CURP (más rápido), validado contra la estructura oficial.
- La foto se guarda como borrador cifrado al leerla y solo se convierte en registro al pulsar Guardar (los borradores caducan a las 2 h).
- Imágenes cifradas en disco (Fernet) y servidas solo a usuarios con sesión.
- Tabla diaria para Operación con miniaturas y filtro por fecha; CSV y borrado solo para admin.
- Protección CSRF, cabeceras de seguridad, cookies seguras, límite de tamaño y validación real de la imagen.
- Comando `purgar` para la política de retención.

## Probar en tu equipo (sin Docker)
Requiere Python 3.10+ y Tesseract con español (`sudo apt install tesseract-ocr tesseract-ocr-spa`, o `brew install tesseract tesseract-lang`).

```bash
pip install -r requirements.txt
export SECRET_KEY=$(python -c "import secrets;print(secrets.token_hex(32))")
export FERNET_KEY=$(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
export COOKIE_SECURE=0          # solo para pruebas locales sin HTTPS
flask --app app crear-usuario admin --rol admin
flask --app app crear-usuario ana --rol captura
flask --app app crear-usuario luis --rol operacion
python app.py                   # http://127.0.0.1:8000
```

## Desplegar para usuarios externos (con Docker)
1. Sube esta carpeta a un repositorio privado o a un servidor con Docker.
2. Genera `SECRET_KEY` y `FERNET_KEY` (ver `.env.example`) y guárdalas en un lugar seguro.
   **Si pierdes `FERNET_KEY`, las imágenes guardadas no se pueden recuperar.**
3. Construye y ejecuta, montando un volumen persistente en `/data`:
   ```bash
   docker build -t ine-app .
   docker run -d --name ine-app -p 8000:8000 -v ine_data:/data \
     -e SECRET_KEY=... -e FERNET_KEY=... ine-app
   ```
4. Crea los usuarios. Sin consola (p. ej. Render): define `ADMIN_USUARIO` y `ADMIN_CLAVE` (y `USUARIOS` en JSON con el campo `"rol"`: `captura`, `operacion` o `admin`) como variables de entorno; la app crea o actualiza esas cuentas al arrancar. Con consola:
   ```bash
   docker exec -it ine-app flask --app app crear-usuario admin --rol admin
   docker exec -it ine-app flask --app app crear-usuario ana --rol captura
   ```
5. Publica con **HTTPS** (obligatorio: la cookie de sesión es `Secure`). Opciones:
   - Un servicio como Render, Railway o Fly.io (despliegan el Dockerfile, dan HTTPS y permiten un disco persistente montado en `/data`).
   - Un VPS con un proxy inverso (Caddy o Nginx) que termine TLS y apunte al puerto 8000.
6. Entrega a cada usuario externo su enlace, su usuario y su contraseña.

## Actualizar desde la versión anterior
La base de datos se migra sola al arrancar: los usuarios con `es_admin` pasan a rol `admin`, los demás a `captura`, y los registros viejos siguen visibles (sus columnas nombre/vigencia se conservan pero ya no se usan ni se muestran). Si defines `USUARIOS`, agrega `"rol"` a cada uno. Los registros antiguos tienen su hora en UTC; los nuevos usan `ZONA_HORARIA` (por defecto `America/Mexico_City`).

## Retención de datos
```bash
docker exec ine-app flask --app app purgar --dias 30
```
Ejecútalo con un cron o tarea programada del hosting para borrar consultas e imágenes con más de N días.

## Respaldo
Respalda el volumen `/data` (contiene `consultas_ine.db` y `credenciales/`) y guarda `FERNET_KEY` por separado.

## Antes de abrirla al público
Una credencial INE contiene datos personales (en México, regulados por la LFPDPPP). Antes de usarla con terceros:
- Publica un **aviso de privacidad** y obtén el consentimiento de quienes suban su credencial.
- Define cuánto tiempo conservarás los datos y aplica `purgar`.
- Limita los usuarios y revisa quién tiene rol admin.
- Esta app es un punto de partida; no incluye auditoría detallada, doble factor ni recuperación de contraseña. Si manejas volumen real, pide una revisión de seguridad.

## Mejorar la lectura del OCR
El OCR hace hasta cuatro pasadas con Tesseract (imagen normal y con el fondo atenuado, en distintos modos de segmentación) y se detiene en cuanto tiene un CURP confiable. Si una credencial no se lee bien:
- Pon `OCR_DEBUG=1` en las variables de entorno, repite la consulta y revisa en **Logs** qué texto leyó cada pasada. Ese texto contiene datos personales: quita la variable al terminar.
- Comprueba que el idioma `spa` esté instalado (el `Dockerfile` ya lo instala). Si el servidor no lo tiene, define `OCR_LANG=eng`.
- Una foto de frente, sin reflejos del holograma ni sombras, mejora más la lectura que cualquier ajuste.
- Las credenciales muy desgastadas o con fondo muy cargado pueden seguir fallando: el CURP se valida contra su estructura oficial y la app avisa cuando no la cumple.

## Velocidad
Cada consulta escribe en los **Logs** una línea como `OCR: 3.2 s, 2 pasada(s), imagen 2000x1250` (sin datos personales). Casi todo el tiempo es Tesseract: cada pasada cuesta aproximadamente lo mismo, y el tiempo real depende de la CPU del plan de tu hosting. Para ir más rápido: sube a un plan con más CPU, o baja `OCR_MAX_PASADAS` (por ejemplo a `2`) aceptando que las fotos difíciles se lean peor.

## Notas técnicas
- Se ejecuta con 1 proceso de gunicorn y varios hilos, porque el limitador de intentos de login vive en memoria. Si escalas a varios procesos o servidores, muévelo a Redis o similar.
- SQLite es suficiente para pocos usuarios simultáneos; para más carga, migra a PostgreSQL.
- La precisión del OCR depende de la foto: de frente, nítida y bien iluminada.
