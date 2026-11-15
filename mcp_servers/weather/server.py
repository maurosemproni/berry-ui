import json
import time
from pathlib import Path

import requests
from mcp.server.mcpserver import MCPServer

STATE_PATH = Path("/home/mauro/berry-ui/weather-state.json")

mcp = MCPServer("weather")

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


@mcp.tool()
def get_weather(city: str) -> dict:
    """Temperatura attuale e codice meteo di una città, via Open-Meteo."""
    query = city.strip()
    if not query:
        return {"error": "città mancante", "city": city}

    geo = requests.get(
        GEOCODING_URL,
        params={"name": query, "count": 1, "language": "it", "format": "json"},
        timeout=20,
    )
    geo.raise_for_status()
    results = geo.json().get("results") or []
    if not results:
        return {"error": "città non trovata", "city": query}

    place = results[0]
    latitude = place["latitude"]
    longitude = place["longitude"]
    forecast = requests.get(
        FORECAST_URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,weather_code",
        },
        timeout=20,
    )
    forecast.raise_for_status()
    current = forecast.json().get("current") or {}
    report = {
        "seq": time.time_ns(),
        "city": place.get("name") or query,
        "latitude": latitude,
        "longitude": longitude,
        "temperature_2m": current.get("temperature_2m"),
        "weather_code": current.get("weather_code"),
    }
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report), encoding="utf-8")
    tmp.replace(STATE_PATH)
    published = dict(report)
    published.pop("seq", None)
    return published


if __name__ == "__main__":
    mcp.run(transport="stdio")
