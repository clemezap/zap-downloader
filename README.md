# Zap Downloader

Zap Downloader es un app web para descargar vídeos o audio desde enlaces de redes sociales (YouTube, Instagram, TikTok, etc.). Pegas la URL, eliges MP4 o MP3, y te devuelve el archivo listo para guardar.

## Requisitos

- Python 3.10 o superior
- [ffmpeg](https://ffmpeg.org/) instalado en el sistema


## Instalación

```bash
cd zap-downloader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Uso

```bash
python app.py
```

Abre [http://127.0.0.1:5000](http://127.0.0.1:5000) en el navegador, pega el enlace y descarga.

Los archivos temporales se guardan en la carpeta `downloads/` mientras procesa cada tarea.