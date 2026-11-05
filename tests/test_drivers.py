"""Driver tests against fake I2C buses (no hardware)."""

from __future__ import annotations

import ctypes

import pytest

from rotse_weather_mast.config import HeaterConfig
from rotse_weather_mast.sensors import SkipReading, dew_point_c
from rotse_weather_mast.sensors.bme280 import BME280
from rotse_weather_mast.sensors.sht45 import CMD_MEASURE_HIGH, CMD_READ_SERIAL, HEATER_CMDS, SHT45, convert, crc8

I2C_M_RD = 0x0001


def test_crc8_datasheet_example():
    assert crc8(b"\xbe\xef") == 0x92  # SHT4x datasheet example


def _frame(s_t: int, s_rh: int) -> bytes:
    t, rh = s_t.to_bytes(2, "big"), s_rh.to_bytes(2, "big")
    return t + bytes([crc8(t)]) + rh + bytes([crc8(rh)])


def test_convert():
    t, rh = convert(_frame(0x6666, 0x8000))  # 0x6666/65535 = 0.4
    assert t == pytest.approx(-45 + 175 * 0.4, abs=1e-3)
    assert rh == pytest.approx(-6 + 125 * 0x8000 / 65535, abs=1e-3)
    assert convert(_frame(0, 0))[1] == 0.0  # RH clipped at 0
    assert convert(_frame(0, 0xFFFF))[1] == 100.0  # and at 100


def test_convert_bad_crc():
    raw = bytearray(_frame(0x6666, 0x8000))
    raw[2] ^= 0xFF
    with pytest.raises(OSError, match="CRC"):
        convert(bytes(raw))


class FakeSHT45Bus:
    def __init__(self, s_t: int = 0x6666, s_rh: int = 0x8000) -> None:
        self.s_t, self.s_rh = s_t, s_rh
        self.commands: list[int] = []

    def i2c_rdwr(self, *msgs) -> None:
        for msg in msgs:
            if msg.flags & I2C_M_RD:
                cmd = self.commands[-1]
                if cmd == CMD_READ_SERIAL:
                    data = _frame(0x1234, 0x5678)
                else:  # a measurement or a heater pulse
                    data = _frame(self.s_t, self.s_rh)
                ctypes.memmove(msg.buf, data, len(data))
            else:
                self.commands.append(msg.buf[0][0])

    def close(self) -> None:
        pass


def _sht(bus: FakeSHT45Bus, **heater) -> tuple[SHT45, list[float]]:
    now = [1000.0]
    sensor = SHT45("sht45", bus, HeaterConfig(**heater), sleep=lambda s: None, clock=lambda: now[0])
    return sensor, now


def test_sht45_read_and_serial():
    bus = FakeSHT45Bus()
    sensor, _ = _sht(bus)
    assert sensor.serial == "12345678"
    v = sensor.read()
    assert v["temp_c"] == pytest.approx(25.0, abs=0.01)
    assert set(v) == {"temp_c", "rh_pct"}
    assert bus.commands == [CMD_READ_SERIAL, CMD_MEASURE_HIGH]


def test_sht45_heater_pulse_and_discard():
    bus = FakeSHT45Bus(s_rh=0xFFFF)  # 100 % RH
    sensor, now = _sht(bus, hold_s=60, power="high", pulse_s=1.0)
    sensor.read()  # starts the hold timer
    now[0] += 61
    sensor.read()  # held long enough -> pulse
    assert bus.commands[-1] == HEATER_CMDS[("high", 1.0)]
    with pytest.raises(SkipReading):
        sensor.read()  # biased by the heater
    sensor.read()
    assert sensor.heater.pulses == 1


def test_sht45_heater_disabled():
    bus = FakeSHT45Bus(s_rh=0xFFFF)
    sensor, now = _sht(bus, enabled=False, hold_s=0)
    for _ in range(3):
        sensor.read()
        now[0] += 100
    assert sensor.heater.pulses == 0
    assert all(c in (CMD_READ_SERIAL, CMD_MEASURE_HIGH) for c in bus.commands)


class FakeBME280Bus:
    """Registers loaded with the Bosch BMP280 datasheet compensation example
    (T1..T3, P1..P9, adc_T = 519888, adc_P = 415148 -> 25.08 degC, 100653.27 Pa)."""

    def __init__(self, chip_id: int = 0x60) -> None:
        self.mem = bytearray(256)
        self.mem[0xD0] = chip_id
        for reg, val in zip(range(0x88, 0xA0, 2), [27504, 26435, -1000, 36477, -10685, 3024, 2855, 140, -7, 15500, -14600, 6000]):
            self.mem[reg : reg + 2] = (val & 0xFFFF).to_bytes(2, "little")
        self.mem[0xA1] = 75  # H1..H6: plausible values (the example has no humidity)
        self.mem[0xE1:0xE3] = (362).to_bytes(2, "little")
        self.mem[0xE3] = 0
        self.mem[0xE4], self.mem[0xE5], self.mem[0xE6] = 0x13, 0x22, 0x03  # H4 = 306, H5 = 50
        self.mem[0xE7] = 30
        self.mem[0xF7:0xFA] = (415148 << 4).to_bytes(3, "big")
        self.mem[0xFA:0xFD] = (519888 << 4).to_bytes(3, "big")
        self.mem[0xFD:0xFF] = (30000).to_bytes(2, "big")
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise OSError(121, "Remote I/O error")

    def read_byte_data(self, addr: int, reg: int) -> int:
        self._check()
        return self.mem[reg]

    def read_word_data(self, addr: int, reg: int) -> int:
        self._check()
        return self.mem[reg] | self.mem[reg + 1] << 8

    def write_byte_data(self, addr: int, reg: int, val: int) -> None:
        self._check()

    def read_i2c_block_data(self, addr: int, reg: int, n: int) -> list[int]:
        self._check()
        return list(self.mem[reg : reg + n])


def test_bme280_datasheet_example():
    sensor = BME280("bme280", FakeBME280Bus())
    v = sensor.read()
    assert v["temp_c"] == pytest.approx(25.08, abs=0.01)
    assert v["pressure_hpa"] == pytest.approx(1006.53, abs=0.01)
    assert 0.0 <= v["rh_pct"] <= 100.0
    assert sensor.status()["chip_id"] == "0x60"


def test_bme280_rejects_bmp280():
    with pytest.raises(RuntimeError, match="BMP280"):
        BME280("bme280", FakeBME280Bus(chip_id=0x58)).read()


def test_bme280_recalibrates_after_bus_error():
    bus = FakeBME280Bus()
    sensor = BME280("bme280", bus)
    sensor.read()
    bus.fail = True
    with pytest.raises(OSError):
        sensor.read()
    assert sensor.status()["calibrated"] is False
    bus.fail = False
    assert sensor.read()["temp_c"] == pytest.approx(25.08, abs=0.01)


def test_dew_point():
    assert dew_point_c(20.0, 100.0) == pytest.approx(20.0, abs=0.01)
    assert dew_point_c(20.0, 50.0) == pytest.approx(9.26, abs=0.05)
