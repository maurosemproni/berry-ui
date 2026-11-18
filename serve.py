#!/usr/bin/env python3
"""Serve la cartella di Berry senza cache e sintetizza la voce delle risposte."""

import json
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

ROOT = "/home/mauro/berry-ui"
BIND = "127.0.0.1"
PORT = 8765
TURN_PATH = Path("/tmp/berry_turn.json")
METRICS_PATH = Path("/tmp/berry_metrics.json")
SESSIONS_PATH = Path.home() / ".openclaw/agents/main/sessions/sessions.json"
METRIC_KEYS = ("wake_word_ms", "stt_ms", "llm_ms", "tts_ms", "tokens")
REPLY_MAX_S = 120
MAX_TTS_CHARS = 500
TTS_TIMEOUT_S = 20
EDGE_TTS = "/home/mauro/berry-ui/node_modules/node-edge-tts/bin.js"
NODE = shutil.which("node") or "/usr/bin/node"


def empty_metrics():
    metrics = {key: 0 for key in METRIC_KEYS}
    metrics["reply_avg_s"] = 0
    return metrics


def load_metrics_file():
    try:
        loaded = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def reply_stats(loaded):
    count = loaded.get("reply_count", 0)
    total = loaded.get("reply_sum_s", 0)
    if isinstance(count, bool) or not isinstance(count, (int, float)) or count < 0:
        count = 0
    if isinstance(total, bool) or not isinstance(total, (int, float)) or total < 0:
        total = 0
    return int(count), float(total)


def reply_average(count, total):
    if count <= 0:
        return 0
    return round(total / count, 1)


def save_metrics(metrics):
    tmp = METRICS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(metrics) + "\n", encoding="utf-8")
    tmp.replace(METRICS_PATH)


def read_metrics():
    metrics = empty_metrics()
    loaded = load_metrics_file()
    for key in METRIC_KEYS:
        value = loaded.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        metrics[key] = max(0, int(value))
    count, total = reply_stats(loaded)
    metrics["reply_avg_s"] = reply_average(count, total)
    return metrics


def cpu_temp():
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text(encoding="utf-8")
        return round(int(raw.strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return "N/A"


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number / 1000.0 if number > 1e12 else number
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def trajectory_path():
    try:
        sessions = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = sessions.get("agent:main:main") if isinstance(sessions, dict) else None
    session_file = entry.get("sessionFile") if isinstance(entry, dict) else None
    if not session_file:
        return None
    path = Path(session_file).with_suffix(".trajectory.jsonl")
    return path if path.is_file() else None


def llm_from_trajectory(sent_at):
    path = trajectory_path()
    if path is None or sent_at <= 0:
        return 0, 0
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - 1_500_000))
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return 0, 0
    lines = raw.splitlines()
    if size > 1_500_000 and lines:
        lines = lines[1:]
    tokens = 0
    last = None
    for line in lines:
        if '"type":"model.completed"' not in line and '"type": "model.completed"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "model.completed":
            continue
        when = _epoch(event.get("ts"))
        if when is None or when < sent_at:
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        prompt = str(data.get("finalPromptText") or "")
        if "heartbeat" in prompt.lower():
            continue
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        total = usage.get("total") or 0
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            tokens += int(total)
        last = when
    llm_ms = max(0, int((last - sent_at) * 1000)) if last is not None else 0
    return llm_ms, tokens


def write_metrics(tts_ms):
    turn = {}
    try:
        loaded = json.loads(TURN_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            turn = loaded
    except (OSError, json.JSONDecodeError):
        turn = {}
    sent_at = turn.get("sent_at") or 0
    if isinstance(sent_at, bool) or not isinstance(sent_at, (int, float)):
        sent_at = 0
    llm_ms, tokens = llm_from_trajectory(float(sent_at))
    metrics = empty_metrics()
    for key in ("wake_word_ms", "stt_ms"):
        value = turn.get(key, 0)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metrics[key] = max(0, int(value))
    metrics["llm_ms"] = llm_ms
    metrics["tts_ms"] = max(0, int(tts_ms))
    metrics["tokens"] = tokens
    count, total = reply_stats(load_metrics_file())
    metrics["reply_count"] = count
    metrics["reply_sum_s"] = total
    metrics["reply_avg_s"] = reply_average(count, total)
    save_metrics(metrics)


def record_reply_latency():
    try:
        turn = json.loads(TURN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return
    if not isinstance(turn, dict):
        return
    sent_at = turn.get("sent_at") or 0
    if isinstance(sent_at, bool) or not isinstance(sent_at, (int, float)) or sent_at <= 0:
        return
    elapsed = time.time() - float(sent_at)
    if elapsed < 0 or elapsed > REPLY_MAX_S:
        return
    metrics = load_metrics_file()
    count, total = reply_stats(metrics)
    count += 1
    total += elapsed
    metrics["reply_count"] = count
    metrics["reply_sum_s"] = total
    metrics["reply_avg_s"] = reply_average(count, total)
    save_metrics(metrics)
    turn["sent_at"] = 0
    tmp = TURN_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(turn) + "\n", encoding="utf-8")
    tmp.replace(TURN_PATH)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def _drop_validators(self):
        for key in ("If-Modified-Since", "If-None-Match"):
            if key in self.headers:
                del self.headers[key]

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/telemetry":
            self._telemetry()
            return
        self._drop_validators()
        super().do_GET()

    def _telemetry(self):
        body = {
            "hardware": {
                "cpu": psutil.cpu_percent(interval=0.1),
                "ram": psutil.virtual_memory().percent,
                "temp": cpu_temp(),
            },
            "latency_pipeline": read_metrics(),
        }
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_HEAD(self):
        self._drop_validators()
        super().do_HEAD()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/playback":
            self._playback()
            return
        if path == "/reply-latency":
            self._reply_latency()
            return
        if path != "/tts":
            self.send_error(404)
            return
        self._speak()

    def _playback(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 256:
            self.send_error(400, "corpo non valido")
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            speaking = bool(body.get("speaking"))
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            self.send_error(400, "json non valido")
            return
        path = Path(ROOT) / "playback-state.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"speaking": speaking}) + "\n", encoding="utf-8")
        tmp.replace(path)
        self.send_response(204)
        self.end_headers()

    def _reply_latency(self):
        try:
            record_reply_latency()
        except OSError:
            pass
        self.send_response(204)
        self.end_headers()

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
        tts_ms = 0
        for _ in (1, 2):
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp.close()
            audio_path = Path(tmp.name)
            attempt_started = time.time()
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
                tts_ms = int((time.time() - attempt_started) * 1000)
                break
        if completed is None or completed.returncode != 0 or not audio:
            self.send_error(502, "sintesi non riuscita")
            return
        try:
            write_metrics(tts_ms)
        except OSError:
            pass
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
