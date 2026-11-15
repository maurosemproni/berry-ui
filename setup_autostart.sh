#!/usr/bin/env bash
# Configura l'avvio automatico di Berry.
#
# 1. Sessione grafica: Chromium in kiosk sulla faccina.
#    Questo Raspberry usa labwc, non Wayfire: il file letto al login è
#    ~/.config/labwc/autostart. wayfire.ini viene aggiornato solo come
#    copia di riserva.
# 2. Backend: non crea un secondo gateway. Se il servizio utente
#    openclaw-gateway.service è già abilitato, lo lascia com'è.
#    Un servizio di sistema omonimo sulla stessa porta esce con codice 78
#    e resta in loop.
# 3. Il kiosk apre http://127.0.0.1:8765/index.html con --password-store=basic.
#    La pagina deve arrivare da loopback: il gateway rifiuta l'origine file://.
#    Il WebSocket resta su ws://127.0.0.1:18789.
# 4. berry-wake.service ascolta «hey berry» dal microfono e manda il
#    comando al gateway. Senza scheda di cattura resta in attesa.
#
# I percorsi sono assoluti e ricavati dalla cartella dello script e dal PATH.
# Prima di sostituire un file già presente lo script chiede conferma.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INDEX_HTML="${PROJECT_DIR}/index.html"
UI_PORT=8765
UI_URL="http://127.0.0.1:${UI_PORT}/index.html?v=grid2"

RUN_USER="$(id -un)"
RUN_UID="$(id -u)"
RUN_HOME="${HOME}"
CONFIG_DIR="${XDG_CONFIG_HOME:-${RUN_HOME}/.config}"
WAYFIRE_INI="${CONFIG_DIR}/wayfire.ini"
LABWC_AUTOSTART="${CONFIG_DIR}/labwc/autostart"
SYSTEM_LABWC_AUTOSTART="/etc/xdg/labwc/autostart"
USER_UNIT_DIR="${CONFIG_DIR}/systemd/user"
BERRY_UI_UNIT="${USER_UNIT_DIR}/berry-ui.service"
SYSTEM_UNIT="/etc/systemd/system/openclaw.service"
AUTOSTART_KEY="berry"

if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/${RUN_UID}" ]]; then
    export XDG_RUNTIME_DIR="/run/user/${RUN_UID}"
fi

confirm() {
    local prompt="$1"
    local answer

    if [[ ! -t 0 ]]; then
        echo "Terminale non interattivo: non sovrascrivo nulla." >&2
        return 1
    fi

    read -r -p "${prompt} [s/N] " answer
    case "${answer}" in
        s|S|si|sì|Si|Sì|y|Y|yes|Yes) return 0 ;;
        *) return 1 ;;
    esac
}

require_file() {
    local path="$1"
    local label="$2"
    if [[ ! -f "${path}" ]]; then
        echo "Manca ${label}: ${path}" >&2
        exit 1
    fi
}

require_cmd() {
    local name="$1"
    if ! command -v "${name}" >/dev/null 2>&1; then
        echo "Comando non trovato nel PATH: ${name}" >&2
        exit 1
    fi
    command -v "${name}"
}

write_file_confirmed() {
    local path="$1"
    local body="$2"
    local label="$3"

    if [[ -f "${path}" ]] && ! cmp -s <(printf '%s\n' "${body}") "${path}"; then
        echo "${label} esiste già e il contenuto è diverso: ${path}"
        if ! confirm "Sovrascrivere ${path}?"; then
            echo "${label} lasciato invariato."
            return 1
        fi
    elif [[ -f "${path}" ]]; then
        echo "${label} già aggiornato: ${path}"
        return 0
    fi

    mkdir -p "$(dirname "${path}")"
    printf '%s\n' "${body}" > "${path}"
    echo "Scritto ${path}"
    return 0
}

install_labwc_autostart() {
    local kiosk_line="$1"
    local base=""
    local body

    if [[ -f "${SYSTEM_LABWC_AUTOSTART}" ]]; then
        # Il file utente sostituisce quello di sistema: va ricopiato,
        # altrimenti spariscono pannello e desktop.
        base="$(grep -v '/usr/bin/chromium' "${SYSTEM_LABWC_AUTOSTART}" || true)"
    fi

    if [[ -z "${base}" ]]; then
        base="/usr/bin/lxsession-xdg-autostart"
    fi

    # Il kiosk va in background, prima dell'eventuale comando in primo piano.
    body="$(printf '%s\n' "${base}" | awk -v kiosk="${kiosk_line} &" '
        /^[^#[:space:]].*[^&][[:space:]]*$/ && !placed {
            print kiosk
            placed = 1
        }
        { print }
        END { if (!placed) print kiosk }
    ')"

    write_file_confirmed "${LABWC_AUTOSTART}" "${body}" "Autostart labwc" || return 0
    chmod +x "${LABWC_AUTOSTART}" || true
}

install_wayfire_autostart() {
    local desired="$1"
    local body

    if [[ -f "${WAYFIRE_INI}" ]] && grep -qxF "${desired}" "${WAYFIRE_INI}"; then
        echo "Wayfire: autostart già configurato."
        return
    fi

    if [[ ! -f "${WAYFIRE_INI}" ]]; then
        body="$(printf '[autostart]\n%s\n' "${desired}")"
        write_file_confirmed "${WAYFIRE_INI}" "${body}" "wayfire.ini" || return 0
        return
    fi

    if grep -qE "^[[:space:]]*${AUTOSTART_KEY}[[:space:]]*=" "${WAYFIRE_INI}"; then
        echo "In ${WAYFIRE_INI} la chiave '${AUTOSTART_KEY}' ha già un altro comando."
        if ! confirm "Sostituire il comando di autostart '${AUTOSTART_KEY}'?"; then
            echo "wayfire.ini lasciato invariato."
            return
        fi
        local tmp
        tmp="$(mktemp)"
        awk -v key="${AUTOSTART_KEY}" -v line="${desired}" '
            $0 ~ "^[[:space:]]*" key "[[:space:]]*=" { print line; next }
            { print }
        ' "${WAYFIRE_INI}" > "${tmp}"
        mv "${tmp}" "${WAYFIRE_INI}"
        echo "Aggiornato ${WAYFIRE_INI}"
        return
    fi

    if grep -qE '^\[autostart\][[:space:]]*$' "${WAYFIRE_INI}"; then
        local tmp
        tmp="$(mktemp)"
        awk -v line="${desired}" '
            { print }
            /^\[autostart\][[:space:]]*$/ && !inserted { print line; inserted = 1 }
        ' "${WAYFIRE_INI}" > "${tmp}"
        mv "${tmp}" "${WAYFIRE_INI}"
        echo "Aggiunto autostart in ${WAYFIRE_INI}"
        return
    fi

    printf '\n[autostart]\n%s\n' "${desired}" >> "${WAYFIRE_INI}"
    echo "Aggiunta la sezione [autostart] in ${WAYFIRE_INI}"
}

install_ui_service() {
    local body
    body="$(cat <<EOF
[Unit]
Description=Berry UI (pagina statica della faccina)
After=network.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=/usr/bin/python3 ${PROJECT_DIR}/serve.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF
)"

    write_file_confirmed "${BERRY_UI_UNIT}" "${body}" "Servizio berry-ui" || return 0
    systemctl --user daemon-reload
    systemctl --user enable berry-ui.service
    systemctl --user restart berry-ui.service
    echo "Servizio utente berry-ui avviato su ${UI_URL}"
}

fetch_vosk_model() {
    local name="$1"
    local model_dir="${PROJECT_DIR}/wake/models/${name}"
    local model_url="https://alphacephei.com/vosk/models/${name}.zip"
    local archive="${PROJECT_DIR}/wake/models/${name}.zip"

    if [[ -d "${model_dir}/am" && -d "${model_dir}/conf" ]]; then
        return 0
    fi
    mkdir -p "${PROJECT_DIR}/wake/models"
    echo "Scarico il modello Vosk (${model_url})"
    curl -fsSL "${model_url}" -o "${archive}"
    unzip -q -o "${archive}" -d "${PROJECT_DIR}/wake/models"
    rm -f "${archive}"
}

install_wake_service() {
    local venv="${PROJECT_DIR}/wake/.venv"
    local py="${venv}/bin/python"
    local unit="${USER_UNIT_DIR}/berry-wake.service"
    local body

    require_cmd arecord >/dev/null

    if [[ ! -x "${py}" ]]; then
        python3 -m venv "${venv}"
    fi
    "${py}" -m pip install -r "${PROJECT_DIR}/wake/requirements.txt"

    fetch_vosk_model "vosk-model-small-en-us-0.15"
    fetch_vosk_model "vosk-model-small-it-0.22"

    if ! id -nG "${RUN_USER}" | tr ' ' '\n' | grep -qx audio; then
        echo "L'utente ${RUN_USER} non è nel gruppo audio."
        echo "Se il microfono non è leggibile: sudo usermod -aG audio ${RUN_USER}"
        echo "Poi esci e rientra nella sessione."
    fi

    if ! arecord -l | grep -q 'card '; then
        echo "Nessuna scheda di cattura audio. Collega un microfono USB: il servizio riproverà da solo."
    fi

    body="$(cat <<EOF
[Unit]
Description=Berry wake word (hey berry)
After=network.target openclaw-gateway.service
Wants=openclaw-gateway.service

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=${py} ${PROJECT_DIR}/wake/listen.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
)"

    write_file_confirmed "${unit}" "${body}" "Servizio berry-wake" || return 0
    systemctl --user daemon-reload
    systemctl --user enable berry-wake.service
    systemctl --user restart berry-wake.service
    echo "Servizio utente berry-wake avviato."

    if command -v openclaw >/dev/null 2>&1; then
        if ! openclaw gateway call voicewake.set --params '{"triggers":["hey berry"]}'; then
            echo "voicewake.set non riuscito: berry-wake lo ritenta all'avvio."
        fi
    fi
}

stop_duplicate_system_gateway() {
    if [[ ! -f "${SYSTEM_UNIT}" ]]; then
        return
    fi

    echo "Trovato ${SYSTEM_UNIT}."
    echo "Confligge con il gateway utente già in ascolto sulla porta 18789."
    if ! confirm "Disabilitare e rimuovere il servizio di sistema openclaw?"; then
        echo "Servizio di sistema lasciato invariato."
        return
    fi

    sudo systemctl disable --now openclaw.service || true
    sudo rm -f "${SYSTEM_UNIT}"
    sudo systemctl daemon-reload
    sudo systemctl reset-failed openclaw.service || true
    echo "Rimosso il servizio di sistema duplicato."
}

require_file "${INDEX_HTML}" "index.html"
CHROMIUM_BIN="$(require_cmd chromium)"
require_cmd python3 >/dev/null

KIOSK_CMD="${CHROMIUM_BIN} --kiosk --noerrdialogs --disable-infobars --autoplay-policy=no-user-gesture-required --ozone-platform=wayland --password-store=basic --disable-features=Translate ${UI_URL}"
WAYFIRE_LINE="${AUTOSTART_KEY} = ${KIOSK_CMD}"

echo "Progetto: ${PROJECT_DIR}"
echo "Chromium: ${CHROMIUM_BIN}"
echo "Kiosk: ${UI_URL}"

install_ui_service
install_wake_service

if [[ -x /usr/bin/labwc || -f "${SYSTEM_LABWC_AUTOSTART}" ]]; then
    install_labwc_autostart "${KIOSK_CMD}"
fi
install_wayfire_autostart "${WAYFIRE_LINE}"

if systemctl --user is-enabled openclaw-gateway.service >/dev/null 2>&1; then
    echo "Gateway già gestito da openclaw-gateway.service (utente). Nessun secondo servizio."
    stop_duplicate_system_gateway
else
    echo "openclaw-gateway.service non è abilitato."
    echo "Installalo con: openclaw gateway install"
fi

echo "Fatto. labwc legge l'autostart al prossimo accesso grafico."
