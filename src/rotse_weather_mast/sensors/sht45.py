"""Sensirion SHT45 over I2C (smbus2, no Blinka). Default address 0x44.

Commands, timings, CRC and conversions are from the Sensirion SHT4x datasheet
(section 4, "Operation and Communication"):

* measurement commands return 6 bytes: T msb, T lsb, CRC, RH msb, RH lsb, CRC
* CRC-8: polynomial 0x31, init 0xFF, no reflection, no final XOR (CRC(0xBEEF) = 0x92)
* T  = -45 + 175 * S_T  / 65535   [degC]
* RH =  -6 + 125 * S_RH / 65535   [%], clipped to 0..100 as the datasheet recommends
* a heater command heats for 0.1 s or 1 s and then measures, so it also returns 6 bytes

Cable note: on a long run up the mast keep I2C at 100 kHz (Pi default) or lower and use
twisted pairs (SDA+GND, SCL+GND).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from ..config import HeaterConfig
from .base import Sensor, SkipReading
from .heater import HeaterPolicy

CMD_MEASURE_HIGH = 0xFD  # high repeatability, max 8.3 ms
CMD_READ_SERIAL = 0x89
CMD_SOFT_RESET = 0x94

MEASURE_WAIT_S = 0.010
SERIAL_WAIT_S = 0.002
RESET_WAIT_S = 0.002

# (power, pulse_s) -> command. Power: high 200 mW, medium 110 mW, low 20 mW.
HEATER_CMDS: dict[tuple[str, float], int] = {
    ("high", 1.0): 0x39,
    ("high", 0.1): 0x32,
    ("medium", 1.0): 0x2F,
    ("medium", 0.1): 0x24,
    ("low", 1.0): 0x1E,
    ("low", 0.1): 0x15,
}


class I2CBus(Protocol):
    """The bit of smbus2.SMBus we need (lets tests pass a fake)."""

    def i2c_rdwr(self, *msgs) -> None: ...  # noqa: E704

    def close(self) -> None: ...  # noqa: E704


def crc8(data: bytes) -> int:
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _words(raw: bytes) -> tuple[int, int]:
    """Split a 6-byte reply into two CRC-checked 16-bit words."""
    if len(raw) != 6:
        raise OSError(f"SHT45: expected 6 bytes, got {len(raw)}")
    out = []
    for i in (0, 3):
        word, crc = raw[i : i + 2], raw[i + 2]
        if crc8(word) != crc:
            raise OSError(f"SHT45: CRC mismatch ({word.hex()} crc {crc:02x}, expected {crc8(word):02x})")
        out.append(word[0] << 8 | word[1])
    return out[0], out[1]


def convert(raw: bytes) -> tuple[float, float]:
    s_t, s_rh = _words(raw)
    temp_c = -45.0 + 175.0 * s_t / 65535.0
    rh = -6.0 + 125.0 * s_rh / 65535.0
    return temp_c, min(max(rh, 0.0), 100.0)


class SHT45(Sensor):
    driver = "sht45"

    def __init__(
        self,
        name: str,
        bus: I2CBus,
        heater_cfg: HeaterConfig,
        address: int = 0x44,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(name)
        self._bus = bus
        self._addr = address
        self._sleep = sleep
        self._heater_cfg = heater_cfg
        self.heater = HeaterPolicy(heater_cfg, clock)
        self.serial: str | None = None
        try:
            self.serial = f"{self._serial():08x}"
        except OSError:
            pass  # not fatal: read() will report the error and keep retrying

    def _command(self, cmd: int, wait_s: float, nbytes: int) -> bytes:
        from smbus2 import i2c_msg  # noqa: PLC0415 - keep import cheap for tests

        self._bus.i2c_rdwr(i2c_msg.write(self._addr, [cmd]))
        self._sleep(wait_s)
        if nbytes == 0:
            return b""
        msg = i2c_msg.read(self._addr, nbytes)
        self._bus.i2c_rdwr(msg)
        return bytes(msg)

    def _serial(self) -> int:
        hi, lo = _words(self._command(CMD_READ_SERIAL, SERIAL_WAIT_S, 6))
        return hi << 16 | lo

    def _pulse_heater(self) -> None:
        cmd = HEATER_CMDS[(self._heater_cfg.power, self._heater_cfg.pulse_s)]
        # Heater on for pulse_s, then a measurement (max ~1.1x the pulse); its result is discarded.
        self._command(cmd, self._heater_cfg.pulse_s * 1.1 + 0.01, 6)

    def read(self) -> dict[str, float]:
        temp_c, rh = convert(self._command(CMD_MEASURE_HIGH, MEASURE_WAIT_S, 6))
        if self.serial is None:
            try:
                self.serial = f"{self._serial():08x}"
            except OSError:
                pass
        if self.heater.discard_next:
            self.heater.discard_next = False
            raise SkipReading("discarding first reading after heater pulse")
        if self.heater.observe(rh):
            self._pulse_heater()
        return {"temp_c": round(temp_c, 2), "rh_pct": round(rh, 2)}

    def status(self) -> dict[str, object]:
        return {"address": f"0x{self._addr:02x}", "serial": self.serial, **self.heater.status()}
