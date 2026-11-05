"""Bosch BME280 (T, RH, pressure) over I2C, using the ``RPi.bme280`` package on smbus2.

Address is 0x76 (SDO low) or 0x77 (SDO high; the Adafruit board's default). Check with
``i2cdetect -y 1``.

The sensor runs in forced mode with x1 oversampling: one conversion per poll, then it
sleeps. That keeps self-heating negligible, which matters for the temperature reading.

Many cheap boards sold as "BME280" carry a BMP280, which has no humidity sensor. The chip
ID register (0xD0) tells them apart: 0x60 = BME280, 0x58 = BMP280 (Bosch datasheets).
"""

from __future__ import annotations

from typing import Any

from .base import Sensor

REG_CHIP_ID = 0xD0
CHIP_ID_BME280 = 0x60
CHIP_ID_BMP280 = 0x58


class BME280(Sensor):
    driver = "bme280"

    def __init__(self, name: str, bus: Any, address: int = 0x76) -> None:
        super().__init__(name)
        self._bus = bus
        self._addr = address
        self._cal: Any = None
        self.chip_id: int | None = None

    def _init(self) -> None:
        import bme280  # noqa: PLC0415

        chip_id = self._bus.read_byte_data(self._addr, REG_CHIP_ID)
        if chip_id == CHIP_ID_BMP280:
            raise RuntimeError(f"chip at 0x{self._addr:02x} is a BMP280 (no humidity), not a BME280")
        if chip_id != CHIP_ID_BME280:
            raise RuntimeError(f"unexpected chip id 0x{chip_id:02x} at 0x{self._addr:02x} (BME280 is 0x60)")
        self.chip_id = chip_id
        self._cal = bme280.load_calibration_params(self._bus, self._addr)

    def read(self) -> dict[str, float]:
        import bme280  # noqa: PLC0415

        if self._cal is None:  # (re)initialise lazily so a sensor plugged in later is picked up
            self._init()
        try:
            r = bme280.sample(self._bus, self._addr, self._cal)
        except OSError:
            self._cal = None  # re-read calibration after a bus error (e.g. sensor power-cycled)
            raise
        return {
            "temp_c": round(float(r.temperature), 2),
            "rh_pct": round(float(r.humidity), 2),
            "pressure_hpa": round(float(r.pressure), 2),
        }

    def status(self) -> dict[str, object]:
        return {
            "address": f"0x{self._addr:02x}",
            "chip_id": None if self.chip_id is None else f"0x{self.chip_id:02x}",
            "calibrated": self._cal is not None,
        }
