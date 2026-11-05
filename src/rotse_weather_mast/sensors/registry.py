"""Build sensors from config by ``type``.

Adding a sensor: write a driver (``Sensor`` subclass; import Pi-only libraries lazily),
add a case below, add a simulator and an entry in ``_SIM_EQUIVALENT``, and add its fields
to ``pyobs_weather.field_codes`` if they map to a pyobs-weather sensor type.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..config import Settings
from .base import Sensor

log = logging.getLogger(__name__)

_SIM_EQUIVALENT = {"sht45": "sim_sht45", "bme280": "sim_bme280"}


def open_bus(bus_no: int) -> Any:
    from smbus2 import SMBus  # noqa: PLC0415

    return SMBus(bus_no)


def build_sensors(settings: Settings, sim: bool, bus: Any = None) -> list[Sensor]:
    """``bus`` is opened on demand (once, shared) unless every sensor is simulated."""
    sensors: list[Sensor] = []
    for i, (name, cfg) in enumerate(settings.sensors.items()):
        kind = _SIM_EQUIVALENT.get(cfg.type, cfg.type) if sim else cfg.type
        seed = settings.sim.seed + i
        match kind:
            case "sim_sht45":
                from .sim import SimSHT45

                sensors.append(SimSHT45(name, settings.sim, settings.heater, time.time, seed=seed))
            case "sim_bme280":
                from .sim import SimBME280

                sensors.append(SimBME280(name, settings.sim, time.time, seed=seed))
            case "sht45":
                from .sht45 import SHT45

                bus = bus if bus is not None else open_bus(settings.i2c_bus)
                sensors.append(SHT45(name, bus, settings.heater, address=cfg.address or 0x44))
            case "bme280":
                from .bme280 import BME280

                bus = bus if bus is not None else open_bus(settings.i2c_bus)
                sensors.append(BME280(name, bus, address=cfg.address or 0x76))
            case _:
                raise ValueError(f"unknown sensor type '{kind}' for sensor '{name}'")
        if kind.startswith("sim_") and not sim:
            log.warning("sensor %s uses simulated driver '%s' on real hardware", name, kind)
    return sensors
