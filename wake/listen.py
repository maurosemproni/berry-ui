#!/usr/bin/env python3
"""Ascolta «hey berry» dal microfono e invia il comando al gateway OpenClaw."""

import base64
import hashlib
import json
import os
import re
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, urlretrieve

ROOT = Path(__file__).resolve().parent.parent
WAKE_DIR = Path(__file__).resolve().parent
STATE_PATH = ROOT / "wake-state.json"
PLAYBACK_PATH = ROOT / "playback-state.json"
TURN_PATH = Path("/tmp/berry_turn.json")
TELEMETRY_STATE = ROOT / "telemetry-state.json"
CONFIG_PATH = ROOT / "config.js"
MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_DIR = WAKE_DIR / "models" / MODEL_NAME
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
IT_MODEL_NAME = "vosk-model-small-it-0.22"
IT_MODEL_DIR = WAKE_DIR / "models" / IT_MODEL_NAME
IT_MODEL_URL = f"https://alphacephei.com/vosk/models/{IT_MODEL_NAME}.zip"

GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 18789
GATEWAY_ORIGIN = "http://127.0.0.1:8765"
SESSION_KEY = "agent:main:main"
# Il modello piccolo confonde la vocale: «berry» esce spesso come barry o bury.
# Sul C-Media le prove del 16 nov 2026 sono uscite «a daddy», «a badly»,
# «a bad name», «a battery», «he badly», «behave badly», «a than me»,
# «he then me» e «in barely».
WAKE_ALIASES = (
    "hey berry", "hey barry", "hey bury",
    "a daddy", "a badly", "a bad name", "a battery",
    "he badly", "behave badly", "a than me", "he then me",
    "in barely",
)
WAKE_PREFIXES = WAKE_ALIASES + ("ehi berry", "hey beri", "e berry", "ei berry")
# Il 18 nov 2026 «hey berry» è uscito anche «hey manny», «hey badly»,
# «hey madly» e «hey bernie». Non vale qualsiasi frase di due o tre parole:
# «what's that» era «basta».
_HEY_MISHEAR = re.compile(r" (hey) ([bm][a-z]{2,8}(?:ie|y)) ")
TRIGGERS = ["hey berry"]

SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # 100 ms, mono, s16le
BACKLOG_S = 3.0
BACKLOG_BYTES = int(SAMPLE_RATE * 2 * BACKLOG_S)
SILENCE_RMS = 450
SILENCE_S = 1.2
MAX_CAPTURE_S = 12.0
FOLLOWUP_ARM_S = 4.0
COOLDOWN_S = 1.0
RETRY_S = 3.0
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_WORD = re.compile(r"[^a-z0-9\s]+")
_SPACE = re.compile(r"\s+")


def log(message):
    print(f"[berry-wake] {message}", flush=True)


def normalize_phrase(text):
    cleaned = _WORD.sub(" ", (text or "").lower())
    return _SPACE.sub(" ", cleaned).strip()


def find_wake(text):
    """Indice e alias trovati nel testo normalizzato, oppure None."""
    norm = normalize_phrase(text)
    padded = f" {norm} "
    found = None
    for alias in WAKE_ALIASES:
        at = padded.find(f" {alias} ")
        if at >= 0 and (found is None or at < found[0]):
            found = (at, alias)
    mishear = _HEY_MISHEAR.search(padded)
    if mishear and (found is None or mishear.start() < found[0]):
        found = (mishear.start(), f"{mishear.group(1)} {mishear.group(2)}")
    return found


def contains_wake(text):
    return find_wake(text) is not None


def command_after_wake(text):
    """Testo dopo la wake word. None se la frase non c'è, stringa vuota se c'è solo lei."""
    found = find_wake(text)
    if found is None:
        return None
    at, alias = found
    padded = f" {normalize_phrase(text)} "
    return padded[at + len(alias) + 1:].strip()


def strip_wake_prefix(text):
    """Toglie una wake word iniziale. Il resto è il comando, anche senza alias."""
    norm = normalize_phrase(text)
    for prefix in sorted(WAKE_PREFIXES, key=len, reverse=True):
        if norm == prefix:
            return ""
        if norm.startswith(prefix + " "):
            return norm[len(prefix) + 1:].strip()
    return norm


def rms(pcm):
    count = len(pcm) // 2
    if count == 0:
        return 0.0
    total = 0
    for offset in range(0, count * 2, 2):
        sample = int.from_bytes(pcm[offset:offset + 2], "little", signed=True)
        total += sample * sample
    return (total / count) ** 0.5


_last_state = "idle"
_last_command = ""
_last_command_at = 0


def write_state(state, command=None):
    global _last_state, _last_command, _last_command_at
    _last_state = state
    if command is not None:
        _last_command = command
        _last_command_at = int(time.time() * 1000)
    payload = json.dumps({
        "state": _last_state,
        "command": _last_command,
        "command_at": _last_command_at,
    }) + "\n"
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def load_token():
    text = CONFIG_PATH.read_text(encoding="utf-8")
    match = re.search(r'BERRY_GATEWAY_TOKEN\s*=\s*"([^"]+)"', text)
    if not match or not match.group(1).strip():
        raise RuntimeError(f"Token del gateway assente in {CONFIG_PATH}")
    return match.group(1).strip()


def capture_devices():
    proc = subprocess.run(["arecord", "-l"], capture_output=True, text=True, check=False)
    devices = []
    pattern = re.compile(r"card (\d+): (\S+) \[(.*?)\], device (\d+):")
    for line in proc.stdout.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        devices.append({
            "card": int(match.group(1)),
            "id": match.group(2),
            "name": match.group(3),
            "device": int(match.group(4)),
        })
    return devices


def pick_device(devices):
    def playback_only(device):
        blob = f"{device['id']} {device['name']}".lower()
        return "hdmi" in blob or "headphone" in blob

    mics = [device for device in devices if not playback_only(device)]
    if mics:
        return mics[0]
    return devices[0] if devices else None


def arecord_command(device):
    plug = f"plughw:{device['card']},{device['device']}"
    return [
        "arecord",
        "-D", plug,
        "-f", "S16_LE",
        "-r", str(SAMPLE_RATE),
        "-c", "1",
        "-t", "raw",
        "-F", "100000",
        "-q",
    ]


class GatewayClient:
    def __init__(self, token):
        self.token = token
        self.sock = None
        self.buf = b""

    def __enter__(self):
        self.sock = socket.create_connection((GATEWAY_HOST, GATEWAY_PORT), timeout=10)
        self.sock.settimeout(10)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {GATEWAY_HOST}:{GATEWAY_PORT}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: {GATEWAY_ORIGIN}\r\n"
            f"\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        raw = self._read_http_head()
        status = raw.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise ConnectionError(status.decode("utf-8", "replace"))
        expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        accept = ""
        for line in raw.split(b"\r\n")[1:]:
            if line.lower().startswith(b"sec-websocket-accept:"):
                accept = line.split(b":", 1)[1].strip().decode("ascii")
        if accept != expected:
            raise ConnectionError("handshake websocket non valido")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.sock is not None:
                self._send_frame(0x8, b"")
        except OSError:
            pass
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        return False

    def _read_http_head(self):
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake chiuso")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        return head

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def send_json(self, payload):
        self._send_frame(0x1, json.dumps(payload).encode("utf-8"))

    def _need(self, size):
        while len(self.buf) < size:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("connessione chiusa")
            self.buf += chunk
        data = self.buf[:size]
        self.buf = self.buf[size:]
        return data

    def recv_json(self):
        while True:
            header = self._need(2)
            opcode = header[0] & 0x0F
            length = header[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._need(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._need(8))[0]
            if header[1] & 0x80:
                mask = self._need(4)
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(self._need(length)))
            else:
                payload = self._need(length)
            if opcode == 0x1:
                return json.loads(payload.decode("utf-8"))
            if opcode == 0x8:
                raise ConnectionError("websocket chiuso dal gateway")
            if opcode == 0x9:
                self._send_frame(0xA, payload)

    def request(self, method, params, timeout=10):
        req_id = "wake-" + uuid.uuid4().hex
        deadline = time.monotonic() + timeout
        self.send_json({"type": "req", "id": req_id, "method": method, "params": params})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"niente risposta a {method}")
            self.sock.settimeout(remaining)
            frame = self.recv_json()
            if frame.get("type") == "res" and frame.get("id") == req_id:
                return frame


def connect_params(token):
    return {
        "minProtocol": 4,
        "maxProtocol": 4,
        "client": {
            "id": "openclaw-control-ui",
            "version": "berry-wake",
            "platform": "linux",
            "mode": "webchat",
        },
        "role": "operator",
        "scopes": ["operator.read", "operator.write"],
        "caps": [],
        "auth": {"token": token},
        "locale": "it-IT",
        "userAgent": "berry-wake",
    }


def open_gateway(token):
    client = GatewayClient(token)
    client.__enter__()
    try:
        deadline = time.monotonic() + 10
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("challenge di connessione assente")
            client.sock.settimeout(remaining)
            frame = client.recv_json()
            if frame.get("type") == "event" and frame.get("event") == "connect.challenge":
                break
        response = client.request("connect", connect_params(token), timeout=10)
    except Exception:
        client.__exit__(None, None, None)
        raise
    if not response.get("ok"):
        client.__exit__(None, None, None)
        error = response.get("error") or response
        raise ConnectionError(f"connect rifiutato: {error}")
    payload = response.get("payload") or {}
    if payload.get("type") != "hello-ok":
        client.__exit__(None, None, None)
        raise ConnectionError(f"handshake inatteso: {payload}")
    return client, payload


def set_triggers(token):
    client, _payload = open_gateway(token)
    try:
        response = client.request("voicewake.set", {"triggers": TRIGGERS}, timeout=10)
    finally:
        client.__exit__(None, None, None)
    if not response.get("ok"):
        raise ConnectionError(f"voicewake.set rifiutato: {response.get('error') or response}")
    triggers = (response.get("payload") or {}).get("triggers") or TRIGGERS
    log("wake word del gateway: " + ", ".join(triggers))


WEATHER_WORDS = re.compile(r"\b(meteo|previsioni|tempo)\b", re.IGNORECASE)
WEATHER_FILLER = {
    "previsioni", "meteo", "tempo", "domani", "oggi", "per", "a", "di",
    "il", "la", "che", "fa", "le", "del", "della", "un", "una",
}
WEATHER_STATE = ROOT / "weather-state.json"
TELEMETRY_WORDS = re.compile(r"telemetria", re.IGNORECASE)


def weather_city(message):
    words = re.findall(r"[a-zàèéìòù]+", message.lower())
    places = [word for word in words if word not in WEATHER_FILLER]
    return places[-1].capitalize() if places else "Roma"


def _write_weather_state(report):
    tmp = WEATHER_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report), encoding="utf-8")
    tmp.replace(WEATHER_STATE)


def _publish_weather(message):
    city = weather_city(message)
    try:
        geo = _weather_json("https://geocoding-api.open-meteo.com/v1/search?" + urlencode({
            "name": city, "count": 1, "language": "it", "format": "json",
        }))
        results = geo.get("results") or []
        if not results and city != "Roma":
            city = "Roma"
            geo = _weather_json("https://geocoding-api.open-meteo.com/v1/search?" + urlencode({
                "name": city, "count": 1, "language": "it", "format": "json",
            }))
            results = geo.get("results") or []
        if not results:
            return
        place = results[0]
        forecast = _weather_json("https://api.open-meteo.com/v1/forecast?" + urlencode({
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,weather_code",
        }))
        current = forecast.get("current") or {}
        _write_weather_state({
            "seq": time.time_ns(),
            "city": place.get("name") or city,
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "temperature_2m": current.get("temperature_2m"),
            "weather_code": current.get("weather_code"),
        })
    except Exception as exc:
        log(f"meteo non pubblicato: {exc}")


def _weather_json(url):
    with urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def publish_weather(message):
    if not WEATHER_WORDS.search(message or ""):
        return
    threading.Thread(target=_publish_weather, args=(message,), daemon=True).start()


def publish_telemetry(message):
    if not TELEMETRY_WORDS.search(message or ""):
        return
    report = {"seq": time.time_ns()}
    tmp = TELEMETRY_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report), encoding="utf-8")
    tmp.replace(TELEMETRY_STATE)


def write_turn(wake_word_ms, stt_ms):
    payload = {
        "wake_word_ms": max(0, int(wake_word_ms)),
        "stt_ms": max(0, int(stt_ms)),
        "sent_at": time.time(),
    }
    tmp = TURN_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    tmp.replace(TURN_PATH)


def send_command(token, message, wake_word_ms=0, stt_ms=0):
    client, payload = open_gateway(token)
    try:
        scopes = ((payload.get("auth") or {}).get("scopes")) or []
        if "operator.write" not in scopes:
            raise ConnectionError("il gateway non ha concesso operator.write")
        response = client.request("chat.send", {
            "sessionKey": SESSION_KEY,
            "message": message,
            "idempotencyKey": str(uuid.uuid4()),
        }, timeout=15)
    finally:
        client.__exit__(None, None, None)
    if not response.get("ok"):
        raise ConnectionError(f"chat.send rifiutato: {response.get('error') or response}")
    log(f"comando inviato: {message}")
    write_turn(wake_word_ms, stt_ms)
    write_state(_last_state, message)
    publish_weather(message)
    publish_telemetry(message)


def ensure_vosk(name, url):
    directory = WAKE_DIR / "models" / name
    if (directory / "am").is_dir() and (directory / "conf").is_dir():
        return directory
    directory.parent.mkdir(parents=True, exist_ok=True)
    archive = directory.parent / f"{name}.zip"
    log(f"scarico il modello {name}")
    urlretrieve(url, archive)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(directory.parent)
    archive.unlink(missing_ok=True)
    if not (directory / "conf").is_dir():
        raise RuntimeError(f"modello incompleto in {directory}")
    return directory


def ensure_model():
    return ensure_vosk(MODEL_NAME, MODEL_URL)


def ensure_it_model():
    return ensure_vosk(IT_MODEL_NAME, IT_MODEL_URL)


def load_pcm_16k(path):
    """Legge un WAV e lo porta a 16 kHz mono s16le."""
    import wave

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        rate = handle.getframerate()
        width = handle.getsampwidth()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise RuntimeError("il WAV di prova deve essere PCM a 16 bit")
    samples = [int.from_bytes(frames[index:index + 2], "little", signed=True) for index in range(0, len(frames), 2)]
    if channels > 1:
        samples = [sum(samples[index:index + channels]) // channels for index in range(0, len(samples), channels)]
    if rate != SAMPLE_RATE and samples:
        resized = []
        position = 0.0
        step = rate / SAMPLE_RATE
        while position < len(samples) - 1:
            left = int(position)
            mix = position - left
            value = samples[left] * (1 - mix) + samples[left + 1] * mix
            resized.append(int(value))
            position += step
        samples = resized
    return b"".join(int(sample).to_bytes(2, "little", signed=True) for sample in samples)


class Spotter:
    def __init__(self, model, it_model):
        from vosk import KaldiRecognizer

        self._recognizer_cls = KaldiRecognizer
        self.model = model
        self.it_model = it_model
        self.mode = "listen"
        self.mode_since = time.monotonic()
        self.silence_since = None
        self.speech_seen = False
        self.parts = []
        self.backlog = bytearray()
        self.rec = KaldiRecognizer(model, SAMPLE_RATE)
        self._heard_playback = False
        self.wake_started_at = None
        self.wake_word_ms = 0
        self.stt_started_at = None
        self.stt_silence_at = None
        self.stt_ms = 0

    def _playback_speaking(self):
        try:
            data = json.loads(PLAYBACK_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        speaking = bool(data.get("speaking"))
        if speaking:
            if not self._heard_playback:
                self._heard_playback = True
            return True
        if self._heard_playback:
            self._heard_playback = False
            self.parts = []
            self.backlog.clear()
            self.rec = self._recognizer_cls(self.model, SAMPLE_RATE)
            self.wake_started_at = None
        return False

    def _push_backlog(self, pcm):
        self.backlog.extend(pcm)
        extra = len(self.backlog) - BACKLOG_BYTES
        if extra > 0:
            del self.backlog[:extra]

    def _replay(self, pcm):
        for offset in range(0, len(pcm), CHUNK_BYTES):
            self.accept(pcm[offset:offset + CHUNK_BYTES])

    def _use_italian(self, replay):
        self.parts = []
        self.rec = self._recognizer_cls(self.it_model, SAMPLE_RATE)
        if replay and self.backlog:
            self._replay(bytes(self.backlog))

    def enter(self, mode):
        now = time.monotonic()
        self.mode = mode
        self.mode_since = now
        self.silence_since = None
        self.speech_seen = False
        if mode == "listen":
            self.parts = []
            self.backlog.clear()
            self.rec = self._recognizer_cls(self.model, SAMPLE_RATE)
            self.wake_started_at = None
            write_state("listening")
        elif mode == "capture":
            self._use_italian(replay=False)
            self.stt_started_at = time.time()
            self.stt_silence_at = None
            write_state("capturing")
        elif mode == "followup":
            self._use_italian(replay=False)
            self.stt_started_at = time.time()
            self.stt_silence_at = None
            write_state("capturing")
        elif mode == "cooldown":
            write_state("listening")

    def current_text(self):
        partial = json.loads(self.rec.PartialResult()).get("partial") or ""
        chunks = list(self.parts)
        if partial:
            chunks.append(partial)
        return " ".join(chunk for chunk in chunks if chunk).strip()

    def flush_text(self):
        final = json.loads(self.rec.FinalResult()).get("text") or ""
        chunks = list(self.parts)
        if final:
            chunks.append(final)
        self.parts = []
        return " ".join(chunk for chunk in chunks if chunk).strip()

    def accept(self, pcm):
        if self.rec.AcceptWaveform(pcm):
            text = json.loads(self.rec.Result()).get("text") or ""
            if text:
                self.parts.append(text)
            return True
        return False

    def feed(self, pcm, token):
        now = time.monotonic()
        if self.mode == "listen" and self._playback_speaking():
            return
        loud = rms(pcm) >= SILENCE_RMS
        if self.mode == "listen":
            self._push_backlog(pcm)

        if self.mode == "cooldown":
            if now - self.mode_since >= COOLDOWN_S:
                self.enter("listen")
            return

        if self.mode == "followup" and not self.speech_seen:
            if loud:
                self.speech_seen = True
            elif now - self.mode_since >= FOLLOWUP_ARM_S:
                log("nessun comando dopo la wake word")
                self.enter("listen")
                return
            if self.speech_seen:
                self.accept(pcm)
            return

        ended = self.accept(pcm)

        if self.mode == "listen":
            if loud and self.wake_started_at is None:
                self.wake_started_at = time.time()
            text = self.current_text()
            if contains_wake(text):
                started = self.wake_started_at or time.time()
                self.wake_word_ms = max(0, int((time.time() - started) * 1000))
                log("wake word riconosciuta")
                self.enter("capture")
                self.speech_seen = loud
            elif ended:
                self.parts = []
                self.wake_started_at = None
            return

        if loud:
            self.silence_since = None
            self.stt_silence_at = None
            self.speech_seen = True
        elif self.silence_since is None:
            self.silence_since = now
            self.stt_silence_at = time.time()
        elif now - self.silence_since >= SILENCE_S or now - self.mode_since >= MAX_CAPTURE_S:
            self.finish(token)
            return
        if now - self.mode_since >= MAX_CAPTURE_S:
            self.finish(token)

    def finish(self, token):
        text = self.flush_text()
        command = strip_wake_prefix(text)
        end = self.stt_silence_at or time.time()
        started = self.stt_started_at or end
        self.stt_ms = max(0, int((end - started) * 1000))
        log(f"trascritto: {text or '(vuoto)'}")
        if not command:
            if self.mode == "followup":
                log("comando vuoto, non invio")
                self.enter("listen")
            else:
                log("wake word senza comando, attendo la frase")
                self.enter("followup")
            return
        try:
            send_command(token, command, self.wake_word_ms, self.stt_ms)
        except (OSError, ConnectionError, TimeoutError, json.JSONDecodeError) as exc:
            log(f"invio non riuscito: {exc}")
        self.enter("cooldown")


def run_listener():
    token = load_token()
    triggers_set = False
    next_trigger_try = 0.0

    def ensure_triggers():
        nonlocal triggers_set, next_trigger_try
        if triggers_set or time.monotonic() < next_trigger_try:
            return
        next_trigger_try = time.monotonic() + 15
        try:
            set_triggers(token)
        except (OSError, ConnectionError, TimeoutError, json.JSONDecodeError) as exc:
            log(f"voicewake.set non riuscito ({exc})")
            return
        triggers_set = True

    from vosk import Model

    en_model = Model(str(ensure_model()))
    it_model = Model(str(ensure_it_model()))
    spotter = None
    while True:
        ensure_triggers()
        device = pick_device(capture_devices())
        if device is None:
            write_state("idle")
            log("nessun microfono di cattura. Collega un microfono USB, riprovo.")
            time.sleep(RETRY_S)
            continue
        command = arecord_command(device)
        log(f"ascolto su {device['name']} ({command[2]})")
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stderr = []
        threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True).start()
        if spotter is None:
            spotter = Spotter(en_model, it_model)
        spotter.enter("listen")
        try:
            while proc.poll() is None:
                ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                if not ready:
                    continue
                pcm = proc.stdout.read(CHUNK_BYTES)
                if not pcm:
                    break
                spotter.feed(pcm, token)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        message = b"".join(stderr).decode("utf-8", "replace").strip()
        if "permission denied" in message.lower() or "access" in message.lower():
            log("microfono non leggibile. Esegui: sudo usermod -aG audio mauro")
            log("Poi esci e rientra nella sessione.")
        elif message:
            log(message.splitlines()[-1])
        write_state("idle")
        time.sleep(RETRY_S)


def run_phrase_tests():
    cases = [
        ("Hey, Berry!", "", True),
        ("hey berry what time is it", "what time is it", True),
        ("turn on the light", None, False),
        ("hey  berry, open the door", "open the door", True),
        ("HEY BERRY", "", True),
        ("they said hey berry please stop", "please stop", True),
        ("hey barry what time is it", "what time is it", True),
        ("hey ber", None, False),
        ("hey bernie", "", True),
        ("hey manny dimmi ciao", "dimmi ciao", True),
        ("hey badly", "", True),
        ("hey madly", "", True),
        ("what's that", None, False),
        ("pasta", None, False),
        ("boston", None, False),
    ]
    failed = 0
    for text, expected, wake in cases:
        got_wake = contains_wake(text)
        got = command_after_wake(text)
        if got_wake != wake or got != expected:
            failed += 1
            log(f"fallito {text!r}: wake={got_wake} comando={got!r}")
    prefixes = [
        ("hey berry dimmi ciao", "dimmi ciao"),
        ("dimmi ciao", "dimmi ciao"),
        ("hey beri accendi la luce", "accendi la luce"),
        ("hey berry", ""),
    ]
    for text, expected in prefixes:
        got = strip_wake_prefix(text)
        if got != expected:
            failed += 1
            log(f"prefisso fallito {text!r}: {got!r}")
    if failed:
        raise SystemExit(1)
    log(f"parser ok ({len(cases)} frasi)")


def run_wav(path):
    from vosk import KaldiRecognizer, Model

    pcm = load_pcm_16k(path)
    rec = KaldiRecognizer(Model(str(ensure_model())), SAMPLE_RATE)
    text = ""
    for offset in range(0, len(pcm), CHUNK_BYTES):
        chunk = pcm[offset:offset + CHUNK_BYTES]
        if rec.AcceptWaveform(chunk):
            piece = json.loads(rec.Result()).get("text") or ""
            if piece:
                text = f"{text} {piece}".strip()
    tail = json.loads(rec.FinalResult()).get("text") or ""
    if tail:
        text = f"{text} {tail}".strip()
    command = command_after_wake(text)
    log(f"trascritto: {text or '(vuoto)'}")
    if command is None:
        log("wake word assente nel WAV")
        raise SystemExit(1)
    log(f"comando: {command or '(solo wake word)'}")


def run_check_gateway():
    token = load_token()
    client, payload = open_gateway(token)
    try:
        scopes = ((payload.get("auth") or {}).get("scopes")) or []
        log("scope: " + ", ".join(scopes))
        response = client.request("voicewake.set", {"triggers": TRIGGERS}, timeout=10)
    finally:
        client.__exit__(None, None, None)
    if not response.get("ok"):
        raise SystemExit(f"voicewake.set rifiutato: {response.get('error')}")
    triggers = (response.get("payload") or {}).get("triggers")
    log("trigger: " + ", ".join(triggers or []))
    if "operator.write" not in scopes or triggers != TRIGGERS:
        raise SystemExit(1)


def main():
    if "--self-test" in sys.argv:
        run_phrase_tests()
        return
    if "--check-gateway" in sys.argv:
        run_check_gateway()
        return
    if "--wav" in sys.argv:
        index = sys.argv.index("--wav")
        if index + 1 >= len(sys.argv):
            raise SystemExit("manca il percorso dopo --wav")
        run_wav(Path(sys.argv[index + 1]))
        return
    try:
        run_listener()
    except KeyboardInterrupt:
        write_state("idle")


if __name__ == "__main__":
    main()
