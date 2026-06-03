import os
import re
import uuid
import threading
import subprocess
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template
import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Registro de tareas activas: { job_id: { status, filename, error } }
jobs: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _browser_cookies_kwarg() -> dict:
    """
    Intenta extraer cookies del navegador disponible en macOS.
    Prueba Chrome → Firefox → Safari en orden.
    Si ninguno funciona, devuelve dict vacío (descarga sin cookies).
    """
    for browser in ["chrome", "firefox", "safari"]:
        try:
            test_opts = {
                "cookiesfrombrowser": (browser,),
                "quiet": True,
                "skip_download": True,
                "logger": type("L", (), {"debug": lambda *_: None,
                                          "warning": lambda *_: None,
                                          "error": lambda *_: None})(),
            }
            with yt_dlp.YoutubeDL(test_opts):
                pass
            return {"cookiesfrombrowser": (browser,)}
        except Exception:
            continue
    return {}


def _probe_video_codec(path: Path) -> str:
    """
    Usa ffprobe para detectar el codec de video del archivo descargado.
    Retorna el nombre del codec (ej. 'h264', 'vp9', 'av1') o '' si falla.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip().lower()
    except Exception:
        return ""


def _remux_to_premiere_mp4(src: Path, dst: Path) -> None:
    """
    Convierte/remuxea src → dst con las siguientes garantías para Premiere Pro:

    1. Video codec H.264 (libx264) — compatible con Premiere en macOS sin plugins.
       Si el stream ya es H.264 usamos 'copy' para no recodificar (más rápido).
    2. Audio codec AAC — estándar en contenedor MP4.
    3. -movflags +faststart — mueve el moov atom al inicio del archivo.
       Sin esto, Premiere y muchos players no pueden pre-visualizar el video
       (reproduce solo audio) porque el índice está al final del archivo.
    4. Pixel format yuv420p — requerido por Premiere; algunos encoders producen
       yuv444p o yuv422p que Premiere no acepta en el preview.
    """
    codec = _probe_video_codec(src)

    # Si ya es H.264 solo hacemos remux (copy), si no, recodificamos a H.264
    vcodec_args = ["-vcodec", "copy"] if codec == "h264" else [
        "-vcodec", "libx264",
        "-crf", "18",          # Calidad casi transparente (0=lossless, 23=default)
        "-preset", "fast",     # Balance velocidad/compresión
        "-pix_fmt", "yuv420p", # Obligatorio para compatibilidad Premiere
    ]

    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        *vcodec_args,
        "-acodec", "aac",
        "-b:a", "320k",        # Audio AAC 320 kbps
        "-movflags", "+faststart",  # ← Mueve moov atom al inicio
        str(dst),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg falló (código {result.returncode}):\n{result.stderr[-800:]}"
        )


def _build_ydl_opts(fmt: str, job_id: str) -> dict:
    """
    Construye las opciones de yt-dlp según el formato solicitado (mp4/mp3).
    Para MP4 descargamos en el mejor formato disponible y luego hacemos
    el post-procesado con ffmpeg directamente (_remux_to_premiere_mp4).
    """
    output_tmpl = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")
    cookie_kwarg = _browser_cookies_kwarg()

    base = {
        "outtmpl": output_tmpl,
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 5,
        **cookie_kwarg,
    }

    if fmt == "mp4":
        base.update({
            # Prioriza H.264+m4a para evitar recodificación; fallback a cualquier
            # combinación video+audio si no hay H.264 disponible.
            "format": (
                "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]"
                "/bestvideo[vcodec^=avc1]+bestaudio"
                "/bestvideo+bestaudio"
                "/best"
            ),
            # yt-dlp hace el merge inicial; el faststart + validación de codec
            # se aplica después con _remux_to_premiere_mp4
            "merge_output_format": "mp4",
            "postprocessors": [
                {"key": "FFmpegMetadata", "add_metadata": True},
            ],
        })
    else:  # mp3
        base.update({
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                },
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ],
            "writethumbnail": True,
        })

    return base


def _sanitize_filename(name: str) -> str:
    """Elimina caracteres problemáticos del nombre de archivo."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    return name.strip()[:200] or "descarga"


def _find_output_file(job_id: str) -> Path | None:
    """Busca el archivo generado por yt-dlp en el directorio de descargas."""
    matches = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))
    matches = [f for f in matches if not f.suffix.endswith(".part")
               and not f.name.endswith(".ytdl")]
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Worker de descarga (hilo separado)
# ---------------------------------------------------------------------------

def _download_worker(job_id: str, url: str, fmt: str):
    jobs[job_id]["status"] = "downloading"
    raw_file: Path | None = None
    try:
        opts = _build_ydl_opts(fmt, job_id)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)

        raw_file = _find_output_file(job_id)
        if raw_file is None:
            raise FileNotFoundError("No se encontró el archivo de salida de yt-dlp.")

        # Nombre genérico corto: "video_XXXXXXXX.mp4" / "audio_XXXXXXXX.mp3"
        short_id = job_id[:8]

        if fmt == "mp4":
            # ── Post-procesado con ffmpeg para garantizar compatibilidad Premiere ──
            jobs[job_id]["status"] = "processing"
            final_path = DOWNLOAD_DIR / f"video_{short_id}.mp4"

            _remux_to_premiere_mp4(raw_file, final_path)

            # Elimina el archivo intermedio de yt-dlp
            try:
                raw_file.unlink()
            except OSError:
                pass
        else:
            # MP3: solo renombrar con nombre genérico
            final_path = DOWNLOAD_DIR / f"audio_{short_id}.mp3"
            raw_file.rename(final_path)

        jobs[job_id]["status"] = "done"
        jobs[job_id]["filename"] = final_path.name

    except Exception as exc:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(exc)
        # Limpia archivos parciales
        for partial in DOWNLOAD_DIR.glob(f"{job_id}.*"):
            try:
                partial.unlink()
            except OSError:
                pass
        if raw_file and raw_file.exists():
            try:
                raw_file.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Rutas Flask
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    fmt = (data.get("format") or "mp4").lower()

    if not url:
        return jsonify({"error": "URL requerida."}), 400
    if fmt not in ("mp4", "mp3"):
        return jsonify({"error": "Formato inválido. Usa mp4 o mp3."}), 400

    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "queued", "filename": None, "error": None}

    thread = threading.Thread(
        target=_download_worker, args=(job_id, url, fmt), daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id}), 202


@app.route("/api/status/<job_id>")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job no encontrado."}), 404
    return jsonify(job)


@app.route("/api/file/<job_id>")
def download_file(job_id: str):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Archivo no disponible."}), 404

    path = DOWNLOAD_DIR / job["filename"]
    if not path.exists():
        return jsonify({"error": "Archivo no encontrado en disco."}), 404

    ext = path.suffix.lower().lstrip(".")
    mime = "video/mp4" if ext == "mp4" else "audio/mpeg"

    response = send_file(
        path,
        mimetype=mime,
        as_attachment=True,
        download_name=job["filename"],
    )
    # Limpia el job después de servir el archivo
    threading.Timer(30, lambda: jobs.pop(job_id, None)).start()
    return response


if __name__ == "__main__":
    # Verifica dependencias críticas al arrancar
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("\n⚠️  ffmpeg no encontrado. Instálalo con: brew install ffmpeg\n")

    app.run(debug=False, host="127.0.0.1", port=5000)