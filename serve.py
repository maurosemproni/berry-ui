#!/usr/bin/env python3
"""Serve la cartella di Berry senza cache e sintetizza la voce delle risposte."""

import json
import shutil
import subprocess
import tempfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = "/home/mauro/berry-ui"
BIND = "127.0.0.1"
PORT = 8765
MAX_TTS_CHARS = 500
TTS_TIMEOUT_S = 20
EDGE_TTS = "/home/mauro/berry-ui/node_modules/node-edge-tts/bin.js"
NODE = shutil.which("node") or "/usr/bin/node"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def _drop_validators(self):
        for key in ("If-Modified-Since", "If-None-Match"):
            if key in self.headers:
                del self.headers[key]

    def do_GET(self):
        self._drop_validators()
        super().do_GET()

    def do_HEAD(self):
        self._drop_validators()
        super().do_HEAD()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/tts":
            self.send_error(404)
            return
        self._speak()

    def _speak(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 8192:
            self.send_error(400, "corpo non valido")
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            text = body.get("text")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            self.send_error(400, "json non valido")
            return
        if not isinstance(text, str):
            self.send_error(400, "testo mancante")
            return
        text = " ".join(text.split())
        if not text or len(text) > MAX_TTS_CHARS:
            self.send_error(400, "testo vuoto o troppo lungo")
            return
        if not Path(EDGE_TTS).is_file():
            self.send_error(500, "sintesi vocale assente")
            return

        audio = b""
        completed = None
        for _ in (1, 2):
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp.close()
            audio_path = Path(tmp.name)
            try:
                completed = subprocess.run(
                    [
                        NODE,
                        EDGE_TTS,
                        "--text",
                        text,
                        "--filepath",
                        str(audio_path),
                        "--voice",
                        "it-IT-DiegoNeural",
                        "--lang",
                        "it-IT",
                        "--timeout",
                        str(TTS_TIMEOUT_S * 1000),
                    ],
                    timeout=TTS_TIMEOUT_S + 5,
                    check=False,
                )
                audio = audio_path.read_bytes() if audio_path.is_file() else b""
            except (OSError, subprocess.TimeoutExpired):
                completed = None
                audio = b""
            finally:
                audio_path.unlink(missing_ok=True)
            if completed is not None and completed.returncode == 0 and audio:
                break
        if completed is None or completed.returncode != 0 or not audio:
            self.send_error(502, "sintesi non riuscita")
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
