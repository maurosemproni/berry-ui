import json
import os
from pathlib import Path

import psutil
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("telemetry")


def _cpu_temp():
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text(encoding="utf-8")
        return round(int(raw.strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return "N/A"


@mcp.tool()
def get_system_status() -> str:
    """Legge hardware e latenze. Usa questo tool per lamentarti se i consumi o i tempi sono alti."""
    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory().percent

    metrics = {"wake_word_ms": 0, "stt_ms": 0, "llm_ms": 0, "tts_ms": 0, "tokens": 0, "reply_avg_s": 0}
    if os.path.exists("/tmp/berry_metrics.json"):
        try:
            with open("/tmp/berry_metrics.json", "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                metrics = loaded
        except (OSError, json.JSONDecodeError):
            pass

    return json.dumps({
        "hardware": {"cpu": cpu, "ram": ram, "temp": _cpu_temp()},
        "latency_pipeline": metrics,
    })


if __name__ == "__main__":
    mcp.run(transport="stdio")
