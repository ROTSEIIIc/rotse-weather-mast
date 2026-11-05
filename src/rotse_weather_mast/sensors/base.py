"""Sensor interface shared by the real drivers and the simulators."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod


class SkipReading(Exception):
    """Raised when there is deliberately no new data (e.g. the read right after an
    SHT45 heater pulse). Not an error: ``ok`` stays true, ``age_s`` keeps growing."""


class Sensor(ABC):
    driver: str = "?"

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    def read(self) -> dict[str, float]:
        """Blocking read; raise on failure. Runs in a worker thread."""

    def status(self) -> dict[str, object]:
        """Extra diagnostics for /health (serial number, heater state, ...)."""
        return {}

    def close(self) -> None:  # noqa: B027 - optional hook
        pass


def dew_point_c(temp_c: float, rh_pct: float) -> float:
    """Dew point via the Magnus formula (Sonntag 1990 constants, -45..60 degC)."""
    a, b = 17.62, 243.12
    rh = min(max(rh_pct, 0.01), 100.0)
    gamma = math.log(rh / 100.0) + a * temp_c / (b + temp_c)
    return b * gamma / (a - gamma)
