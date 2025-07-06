
# Flask Real‑Time Audio Streaming Server

This tiny project shows how to build a **live audio streaming** server
with [Flask](https://flask.palletsprojects.com/) using nothing more than standard HTTP
**chunked transfer** (no WebSockets!).

## How it works
* The route **`/stream`** streams an MP3 file chunk by chunk with a small delay to simulate live radio.
* The main page renders an HTML5 `<audio>` element pointed at the `/stream` endpoint – so any browser can listen.
* Because it's regular HTTP, you can also open `/stream` in players like **VLC**, **mpv** or **ffplay**.

## Setup

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Put your source audio file in the project root and name it **`sample.mp3`** (or change `AUDIO_FILE` in *app.py*).

## Run

```bash
flask --app app run --reload --host 0.0.0.0 --port 5000
```

Open <http://localhost:5000> in your browser (or on any device in the LAN).

## Going further
* Swap the file read for microphone input with **PyAudio** or **sounddevice** to broadcast live.
* Add **Flask‑SocketIO** if you need bidirectional WebSockets or metadata.
* Secure the stream with HTTPS using **`flask run --cert`** (Flask 3 feature) or a reverse proxy.
* Deploy on a VPS; front with **nginx** for caching/SSL, or use **uWSGI/gunicorn** with `--workers 1 --threads 4`.

&copy; 2025
