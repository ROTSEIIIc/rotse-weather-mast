"""Simulated SHT45 and BME280, so everything runs on a laptop with ``--sim``."""

from __future__ import annotations

import math
import random
from collections.abc import Callable

from ..config import HeaterConfig, SimConfig
from .base import Sensor, SkipReading
from .heater import HeaterPolicy

Clock = Callable[[], float]  # unix seconds


def _solar_hour(ts: float, lon: float) -> float:
    return (ts / 3600.0 + lon / 15.0) % 24.0


def diurnal_temp_c(ts: float, lon: float) -> float:
    """Minimum ~04:00, maximum ~15:00 local solar time; Khomas highland-ish range."""
    phase = 2 * math.pi * (_solar_hour(ts, lon) - 9.5) / 24
    return 17.0 + 9.0 * math.sin(phase) + 1.0 * math.sin(2 * phase)


def diurnal_rh_pct(ts: float, lon: float, temp_c: float) -> float:
    rh = 40.0 - 3.0 * (temp_c - 17.0)
    h = _solar_hour(ts, lon)
    if h < 6 or h > 19.5:  # ~40 min humid episode every 4 h of night, to exercise the heater
        rh += 60.0 * max(0.0, math.sin(2 * math.pi * ts / (4 * 3600))) ** 6
    return min(max(rh, 3.0), 100.0)


class _SimBase(Sensor):
    def __init__(self, name: str, sim: SimConfig, clock: Clock, seed: int) -> None:
        super().__init__(name)
        self._sim = sim
        self._clock = clock
        self._rng = random.Random(seed)

    def _maybe_drop(self) -> None:
        p = self._sim.dropout_prob
        if p > 0 and self._rng.random() < p:
            raise OSError(f"simulated I2C error on {self.name} (dropout_prob={p:g})")


class SimSHT45(_SimBase):
    driver = "sim_sht45"

    def __init__(self, name: str, sim: SimConfig, heater_cfg: HeaterConfig, clock: Clock, seed: int = 1) -> None:
        super().__init__(name, sim, clock, seed)
        self.heater = HeaterPolicy(heater_cfg, clock)

    def read(self) -> dict[str, float]:
        self._maybe_drop()
        if self.heater.discard_next:
            self.heater.discard_next = False
            raise SkipReading("discarding first reading after heater pulse")
        ts = self._clock()
        t = diurnal_temp_c(ts, self._sim.lon) + self._rng.gauss(0, 0.05)
        rh = min(100.0, max(0.0, diurnal_rh_pct(ts, self._sim.lon, t) + self._rng.gauss(0, 0.3)))
        self.heater.observe(rh)
        return {"temp_c": round(t, 2), "rh_pct": round(rh, 2)}

    def status(self) -> dict[str, object]:
        return self.heater.status()


class SimBME280(_SimBase):
    driver = "sim_bme280"

    def read(self) -> dict[str, float]:
        self._maybe_drop()
        ts = self._clock()
        g = self._rng.gauss
        t = diurnal_temp_c(ts, self._sim.lon) + 0.4 + g(0, 0.1)  # reads a bit warm, like the real thing
        rh = min(100.0, max(0.0, diurnal_rh_pct(ts, self._sim.lon, t) + g(0, 1.0)))
        # ~1800 m altitude: station pressure ~820 hPa, semidiurnal tide + slow synoptic drift
        p = 820.0 + 1.5 * math.sin(4 * math.pi * ts / 86400) + 3.0 * math.sin(2 * math.pi * ts / (86400 * 3.1))
        return {"temp_c": round(t, 2), "rh_pct": round(rh, 2), "pressure_hpa": round(p + g(0, 0.05), 2)}
