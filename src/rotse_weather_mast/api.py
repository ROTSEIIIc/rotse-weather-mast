"""HTTP API (FastAPI). Read-only; everything comes from the poller's in-memory state.

pyobs-weather's ``JSON`` station (pyobs-weather 2.2.5, ``weather/stations/json.py``) does
``weather[field]`` for every configured field and ``isoparse(weather["time"])``, and skips
the update on any status other than 200. So its documents here are:

* flat, every configured key always present (a missing key raises KeyError mid-update),
* ``time`` = time of the reading (UTC ISO 8601), not the time of the request,
* values always floats (InfluxDB rejects a field whose type changes),
* 503 instead of stale data.

Each sensor is its own document (and its own pyobs-weather station), so a dead BME280
doesn't take the SHT45 values down with it.
"""

from __future__ import annotations

import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import __version__
from .poller import Poller


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _cpu_temp_c() -> float | None:
    p = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        return round(int(p.read_text().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def _unavailable(detail: str) -> JSONResponse:
    return JSONResponse(status_code=503, content={"ok": False, "error": detail})


def create_app(poller: Poller, mode: str = "hardware", lifespan: Any = None) -> FastAPI:
    app = FastAPI(title="rotse-weather-mast", version=__version__, lifespan=lifespan)
    s = poller.settings

    def sensor_doc(name: str) -> dict[str, object]:
        st = poller.state[name]
        age = poller.age_s(name)
        doc: dict[str, object] = {
            **st.values,
            "time": _iso(st.last_ok_ts),
            "age_s": None if age is None else round(age, 1),
            "ok": poller.fresh(name),
        }
        if not doc["ok"]:
            doc["error"] = st.error or f"stale: last reading {doc['age_s']} s ago (max_age_s {s.max_age_s:g})"
        return doc

    @app.get("/api/v1/sensors")
    def sensors() -> dict[str, object]:
        return {
            "station": s.station,
            "time": _iso(poller.clock()),
            "sensors": {name: sensor_doc(name) for name in poller.state},
        }

    @app.get("/api/v1/health")
    def health() -> JSONResponse:
        healthy = poller.loop_healthy()
        hb = poller.heartbeat_ts
        body = {
            "station": s.station,
            "version": __version__,
            "mode": mode,
            "ok": healthy,
            "uptime_s": round(poller.clock() - poller.started_ts),
            "loop_heartbeat_age_s": None if hb is None else round(poller.clock() - hb, 1),
            "cpu_temp_c": _cpu_temp_c(),
            "host": platform.node(),
            "sensors": {
                sensor.name: {
                    "driver": sensor.driver,
                    "ok": poller.fresh(sensor.name),
                    "error": poller.state[sensor.name].error,
                    "reads": poller.state[sensor.name].reads,
                    "failures": poller.state[sensor.name].failures,
                    "skipped": poller.state[sensor.name].skipped,
                    "last_try": _iso(poller.state[sensor.name].last_try_ts),
                    **sensor.status(),
                }
                for sensor in poller.sensors
            },
        }
        return JSONResponse(status_code=200 if healthy else 503, content=body)

    @app.get("/api/v1/pyobs-weather/{name}")
    def pyobs_weather_sensor(name: str) -> JSONResponse:
        """One sensor's fields, renamed via ``pyobs_weather.field_codes``; 503 unless fresh."""
        if name not in poller.state:
            raise HTTPException(404, f"unknown sensor '{name}'")
        st = poller.state[name]
        if not poller.fresh(name):
            return _unavailable(f"sensor '{name}' not ok ({st.error or 'stale'})")
        codes = s.pyobs_weather.field_codes
        out = {codes[k]: float(v) for k, v in st.values.items() if k in codes}
        return JSONResponse({"time": _iso(st.last_ok_ts), **out})

    return app
