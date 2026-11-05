"""SHT4x heater policy (same as allsky-station), shared by the real driver and the simulator.

If RH stays >= threshold for longer than ``hold_s``, request a heater pulse and discard
the reading that follows it (it is biased warm/dry by the heater).
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import HeaterConfig


class HeaterPolicy:
    def __init__(self, cfg: HeaterConfig, clock: Callable[[], float]) -> None:
        self.cfg = cfg
        self._clock = clock
        self._high_since: float | None = None
        self.discard_next = False
        self.pulses = 0
        self.last_pulse_ts: float | None = None

    def observe(self, rh_pct: float) -> bool:
        """Feed a good reading. Returns True if a heater pulse should fire now."""
        if not self.cfg.enabled or rh_pct < self.cfg.rh_threshold_pct:
            self._high_since = None
            return False
        now = self._clock()
        if self._high_since is None:
            self._high_since = now
        if now - self._high_since >= self.cfg.hold_s:
            self._high_since = None  # restart the hold timer after each pulse
            self.discard_next = True
            self.pulses += 1
            self.last_pulse_ts = now
            return True
        return False

    def status(self) -> dict[str, object]:
        return {
            "heater_enabled": self.cfg.enabled,
            "heater_pulses": self.pulses,
            "heater_last_pulse_unix": self.last_pulse_ts,
            "rh_high_since_unix": self._high_since,
        }
