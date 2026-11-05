"""Sensor polling: keep only the latest reading per sensor, in memory.

Semantics (same as allsky-station):
* ``age_s`` = seconds since the last **successful** read (None if never).
* A failed read keeps the last values, sets ``ok`` false and ``error``; ``age_s`` keeps growing.
* A skipped read (after an SHT45 heater pulse) changes nothing except the skip counter.
* The loop never dies because of a sensor.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import Settings
from .sensors import Sensor, SkipReading, dew_point_c

log = logging.getLogger(__name__)


@dataclass
class SensorState:
    values: dict[str, float] = field(default_factory=dict)
    ok: bool = False
    error: str | None = "no reading yet"
    last_ok_ts: float | None = None
    last_try_ts: float | None = None
    reads: int = 0
    failures: int = 0
    skipped: int = 0


class Poller:
    def __init__(self, settings: Settings, sensors: list[Sensor], clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.sensors = sensors
        self.clock = clock
        self.state: dict[str, SensorState] = {s.name: SensorState() for s in sensors}
        self.started_ts = clock()
        self.heartbeat_ts: float | None = None

    def age_s(self, name: str) -> float | None:
        st = self.state[name]
        return None if st.last_ok_ts is None else max(0.0, self.clock() - st.last_ok_ts)

    def fresh(self, name: str) -> bool:
        """ok and not older than max_age_s."""
        age = self.age_s(name)
        return self.state[name].ok and age is not None and age <= self.settings.max_age_s

    async def poll_once(self) -> None:
        for sensor in self.sensors:
            st = self.state[sensor.name]
            st.last_try_ts = self.clock()
            try:
                values = await asyncio.wait_for(asyncio.to_thread(sensor.read), self.settings.read_timeout_s)
            except SkipReading as e:
                st.skipped += 1
                log.info("%s: %s", sensor.name, e)
                continue
            except Exception as e:  # noqa: BLE001 - any driver error just marks the sensor bad
                msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                if st.ok or st.error != msg:
                    log.warning("%s: read failed: %s", sensor.name, msg)
                st.ok, st.error = False, msg
                st.failures += 1
                continue
            values = {k: float(v) for k, v in values.items()}
            if "temp_c" in values and "rh_pct" in values:
                values["dewpoint_c"] = round(dew_point_c(values["temp_c"], values["rh_pct"]), 2)
            if not st.ok and st.last_ok_ts is not None:
                log.info("%s: recovered", sensor.name)
            st.values, st.ok, st.error = values, True, None
            st.last_ok_ts = self.clock()
            st.reads += 1

    async def run(self) -> None:
        while True:
            self.heartbeat_ts = self.clock()
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # defensive: poll_once already isolates sensors
                log.exception("poll loop error")
            self.heartbeat_ts = self.clock()
            await asyncio.sleep(self.settings.poll_interval_s)

    def loop_healthy(self) -> bool:
        """Heartbeat younger than one full cycle with every sensor timing out, plus slack."""
        if self.heartbeat_ts is None:
            return False
        limit = self.settings.poll_interval_s + len(self.sensors) * self.settings.read_timeout_s + 30
        return self.clock() - self.heartbeat_ts < limit

    def close(self) -> None:
        for s in self.sensors:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                log.exception("closing %s", s.name)
