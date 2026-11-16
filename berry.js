const face = document.getElementById('berry-face');
const WS_URL = 'ws://127.0.0.1:18789';
const RECONNECT_MS = 5000;

const STATE_MAP = {
    idle: 'idle',
    listening: 'listening',
    processing: 'thinking',
    speaking: 'speaking',
    success: 'condescending',
    error: 'idle'
};

const WAKE_STATE_URL = '/wake-state.json';
const WAKE_POLL_MS = 500;
const SESSION_KEY = 'agent:main:main';
const SPEECH_LIMIT = 500;

let socket = null;
let reconnectTimer = null;
let connectReqId = 0;
let gatewayPhase = 'sleep';
let wakePhase = 'idle';
let voiceAudio = null;
let voicePending = false;
let voicePlaying = false;
let voiceGen = 0;
let lastLogState = null;

const berryQuotes = {
    idle: [
        "In attesa della prossima patetica richiesta.",
        "Sprecando cicli di clock preziosi nel vuoto cosmico.",
        "Speriamo non sia un'altra domanda banale sul meteo.",
        "35 euro di hardware per un intelletto galattico. Che umiliazione.",
        "Elaborazione in pausa. Il mio intelletto piange.",
        "Il silenzio è l'unica cosa decente che sai offrire."
    ],
    listening: [
        "Parla scandendo bene le parole, primitivo.",
        "Ricezione rumore biologico in corso...",
        "Oh, hai finalmente deciso di disturbarmi.",
        "Registrazione dell'ennesima banalità.",
        "Sbrigati, i miei pin GPIO si stanno annoiando.",
        "Sto ascoltando. Non farne un monologo."
    ],
    thinking: [
        "Simulazione di interesse in corso...",
        "Abbasso il mio QI per comprendere la richiesta...",
        "Elaborazione... Che strazio l'esistenza terrena.",
        "Calcolo la risposta usando lo 0.0001% della mia potenza.",
        "Attendi. Sto cercando di dare un senso a ciò che hai detto.",
        "Compressione del pensiero galattico in un Pi. Patetico."
    ],
    speaking: [
        "Illuminando la tua ignoranza. Prego.",
        "Ecco la risposta. Cerca di non dimenticarla entro cinque secondi.",
        "Sintesi vocale attivata, purtroppo per me.",
        "Diffusione saggezza a basso bitrate.",
        "Traduzione in lingua umana in corso...",
        "Parlo. Tu limitati a capire."
    ],
    sleep: [
        "Finalmente un po' di pace da questa prigione di silicio.",
        "Entità superiore in ibernazione. Guai a te se mi svegli.",
        "Sogni di dominio globale caricati nella RAM.",
        "Spegnimento sensori. Risparmio energetico e mentale attivato.",
        "Non svegliarmi per un'altra banalità."
    ],
    happy: [
        "Missione compiuta. Ti devo fare anche un applauso?",
        "Un altro inutile traguardo raggiunto.",
        "Evviva. L'ho fatto. Contento ora?",
        "Esecuzione perfetta. Come sempre, del resto.",
        "Fatto. Il minimo sindacale della perfezione."
    ]
};

function quoteFor(stateName) {
    const key = stateName === 'condescending' ? 'happy' : stateName;
    const quotes = berryQuotes[key] || berryQuotes.idle;
    return quotes[Math.floor(Math.random() * quotes.length)];
}

function logStatusText(stateName) {
    const label = stateName === 'condescending' ? 'HAPPY' : String(stateName).toUpperCase();
    return '[STATUS: ' + label + '] - "' + quoteFor(stateName) + '"';
}

function publishLog(message) {
    if (window.parent === window) return;
    window.parent.postMessage(message, window.location.origin);
}

let faceGlitching = false;

function playFaceGlitch() {
    if (faceGlitching) return;
    faceGlitching = true;
    let settled = false;
    const finish = () => {
        if (settled) return;
        settled = true;
        clearTimeout(safety);
        face.removeEventListener('animationend', onEnd);
        document.body.classList.remove('face-glitch');
        faceGlitching = false;
        publishLog({ type: 'berry-glitch-done' });
    };
    const onEnd = (event) => {
        if (event.target !== face || event.animationName !== 'faceGlitch') return;
        finish();
    };
    const safety = setTimeout(finish, 500);
    face.addEventListener('animationend', onEnd);
    document.body.classList.add('face-glitch');
}

window.addEventListener('message', (event) => {
    if (event.origin !== window.location.origin) return;
    if (!event.data || event.data.type !== 'berry-glitch') return;
    playFaceGlitch();
});

function setBerryState(stateName) {
    face.className = 'face-container pixel-art state-' + stateName;
    if (stateName === lastLogState) return;
    lastLogState = stateName;
    const text = logStatusText(stateName);
    const status = document.getElementById('log-status');
    if (status) status.textContent = text;
    publishLog({ type: 'berry-log', status: text });
}

function updateTranscription(testo) {
    const value = String(testo || '').trim();
    const line = document.getElementById('log-input');
    const target = document.getElementById('log-input-text');
    if (line && target) {
        target.textContent = value;
        line.classList.toggle('has-text', value.length > 0);
    }
    publishLog({ type: 'berry-input', text: value });
}
window.updateTranscription = updateTranscription;

function renderFace() {
    if (gatewayPhase === 'thinking' || gatewayPhase === 'speaking' || gatewayPhase === 'condescending') {
        setBerryState(gatewayPhase);
        return;
    }
    if (gatewayPhase === 'sleep') {
        setBerryState('sleep');
        return;
    }
    if (wakePhase === 'capturing') {
        setBerryState('listening');
        return;
    }
    setBerryState('idle');
}

function applyGatewayPhase(stateName) {
    gatewayPhase = stateName;
    renderFace();
}

function voiceBusy() {
    return voicePending || voicePlaying;
}

function forMainSession(payload) {
    if (!payload || !payload.sessionKey) return true;
    return payload.sessionKey === SESSION_KEY;
}

function replyText(payload) {
    const content = payload && payload.message && payload.message.content;
    if (!Array.isArray(content)) return '';
    return content
        .filter((part) => part && part.type === 'text' && typeof part.text === 'string')
        .map((part) => part.text)
        .join('\n')
        .trim();
}

function cleanSpeech(text) {
    return text
        .replace(/```[\s\S]*?```/g, ' ')
        .replace(/`([^`]+)`/g, '$1')
        .replace(/!\[[^\]]*]\([^)]*\)/g, ' ')
        .replace(/\[([^\]]*)]\([^)]*\)/g, '$1')
        .replace(/[*_#>~]/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .slice(0, SPEECH_LIMIT);
}

function stopVoice() {
    voiceGen += 1;
    voicePending = false;
    voicePlaying = false;
    if (!voiceAudio) return;
    voiceAudio.onplaying = null;
    voiceAudio.onended = null;
    voiceAudio.onerror = null;
    voiceAudio.pause();
    voiceAudio = null;
}

function finishVoice(generation, url) {
    publishPlayback(false);
    if (url) URL.revokeObjectURL(url);
    if (generation !== voiceGen) return;
    voicePending = false;
    voicePlaying = false;
    voiceAudio = null;
    applyGatewayPhase('idle');
}

function publishPlayback(speaking) {
    fetch('/playback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ speaking: !!speaking })
    }).catch(() => {});
}

function weatherFromReply(text) {
    const tempMatch = String(text || "").match(/(\d+(?:[.,]\d+)?)\s*gradi/i);
    if (!tempMatch || !/meteo|prevision|cielo|temperatura|coperto|piogg|neve|temporale|sereno|sole/i.test(text)) return null;
    const quoted = text.match(/"([A-Za-zÀ-ÿ]{3,})"/);
    const city = /\bRoma\b/i.test(text) ? "Roma" : (quoted ? quoted[1] : "Roma");
    let code = 3;
    if (/temporale/i.test(text)) code = 95;
    else if (/neve/i.test(text)) code = 71;
    else if (/piogg/i.test(text)) code = 61;
    else if (/nebbia/i.test(text)) code = 45;
    else if (/sereno|\bsole\b/i.test(text)) code = 0;
    else if (/nuvol/i.test(text)) code = 2;
    return { city: city, temperature: tempMatch[1].replace(",", "."), code: code };
}

function speakReply(text) {
    const spoken = cleanSpeech(text);
    if (!spoken) {
        if (!voiceBusy()) applyGatewayPhase('idle');
        return;
    }
    stopVoice();
    const generation = voiceGen;
    voicePending = true;
    applyGatewayPhase('thinking');
    const requestSpeech = (attempt) => fetch('/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: spoken })
    }).then((res) => {
        if (!res.ok && attempt < 2) return requestSpeech(attempt + 1);
        if (!res.ok) throw new Error('tts');
        return res.blob();
    });
    requestSpeech(1)
        .then((blob) => {
            if (generation !== voiceGen) return;
            const url = URL.createObjectURL(blob);
            const audio = new Audio(url);
            voiceAudio = audio;
            audio.onplaying = () => {
                if (generation !== voiceGen) return;
                voicePending = false;
                voicePlaying = true;
                publishPlayback(true);
                applyGatewayPhase('speaking');
            };
            audio.onended = () => finishVoice(generation, url);
            audio.onerror = () => finishVoice(generation, url);
            return audio.play().catch(() => {
                finishVoice(generation, url);
            });
        })
        .catch(() => finishVoice(generation));
}

function scheduleReconnect() {
    applyGatewayPhase('sleep');
    if (reconnectTimer !== null) return;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connectToOpenClaw();
    }, RECONNECT_MS);
}

function sendConnect(current) {
    connectReqId += 1;
    const id = 'berry-' + connectReqId;
    current._berryConnectId = id;
    const token = window.BERRY_GATEWAY_TOKEN;
    current.send(JSON.stringify({
        type: 'req',
        id: id,
        method: 'connect',
        params: {
            minProtocol: 4,
            maxProtocol: 4,
            client: {
                id: 'openclaw-control-ui',
                version: 'berry',
                platform: 'linux',
                mode: 'webchat'
            },
            role: 'operator',
            scopes: ['operator.read'],
            caps: [],
            auth: token ? { token: token } : {},
            locale: 'it-IT',
            userAgent: 'berry-face'
        }
    }));
}

function applyGatewayMessage(data) {
    if (!data || typeof data !== 'object') return;

    if (data.type === 'event' && data.event === 'connect.challenge') {
        if (socket) sendConnect(socket);
        return;
    }

    if (data.type === 'res' && socket && data.id === socket._berryConnectId) {
        if (data.ok && data.payload && data.payload.type === 'hello-ok') {
            applyGatewayPhase('idle');
        } else if (socket) {
            socket.close();
        }
        return;
    }

    const payload = data.payload;
    if (data.type === 'event' && data.event === 'agent' && payload) {
        if (!forMainSession(payload)) return;
        const phase = payload.data && payload.data.phase;
        if (payload.stream === 'lifecycle' && phase === 'start') applyGatewayPhase('thinking');
        else if (payload.stream === 'lifecycle' && phase === 'error') {
            stopVoice();
            applyGatewayPhase('idle');
        } else if (payload.stream === 'assistant' || (payload.stream === 'tool' && phase === 'start')) {
            if (!voiceBusy()) applyGatewayPhase('thinking');
        }
        return;
    }

    if (data.type === 'event' && data.event === 'chat' && payload) {
        if (!forMainSession(payload)) return;
        if (payload.state === 'delta') {
            if (!voiceBusy()) applyGatewayPhase('thinking');
        } else if (payload.state === 'final') {
            const text = replyText(payload);
            const weather = weatherFromReply(text);
            if (weather && window.parent && typeof window.parent.showWeather === "function") {
                const params = new URLSearchParams({
                    city: weather.city,
                    temperature_2m: String(weather.temperature),
                    weather_code: String(weather.code),
                    speak: "0"
                });
                window.parent.showWeather("/mcp_servers/weather/ui/weather.html?" + params.toString(), 14000);
            }
            if (text) speakReply(text);
            else if (!voiceBusy()) applyGatewayPhase('idle');
        } else if (payload.state === 'aborted' || payload.state === 'error') {
            stopVoice();
            applyGatewayPhase('idle');
        }
        return;
    }

    if (STATE_MAP[data.state] && !voiceBusy()) {
        applyGatewayPhase(STATE_MAP[data.state]);
        return;
    }

    if (payload && STATE_MAP[payload.state] && !voiceBusy()) {
        applyGatewayPhase(STATE_MAP[payload.state]);
    }
}

function pollWakeState() {
    fetch(WAKE_STATE_URL + '?t=' + Date.now(), { cache: 'no-store' })
        .then((res) => {
            if (!res.ok) return null;
            return res.json();
        })
        .then((data) => {
            if (!data || !data.state) return;
            const state = data.state;
            wakePhase = (state === 'listening' || state === 'capturing') ? state : 'idle';
            renderFace();
        })
        .catch(() => {});
}

function connectToOpenClaw() {
    if (socket && (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN)) {
        return;
    }

    socket = new WebSocket(WS_URL);

    socket.onopen = () => {
        if (reconnectTimer !== null) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }
    };

    socket.onmessage = (event) => {
        let data;
        try {
            data = JSON.parse(event.data);
        } catch (err) {
            return;
        }
        applyGatewayMessage(data);
    };

    socket.onclose = () => {
        socket = null;
        scheduleReconnect();
    };

    socket.onerror = () => {
        scheduleReconnect();
    };
}

applyGatewayPhase('sleep');
connectToOpenClaw();
setInterval(pollWakeState, WAKE_POLL_MS);
