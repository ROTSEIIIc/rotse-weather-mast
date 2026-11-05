"""Config, poller and HTTP API, using simulated or scripted sensors."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rotse_weather_mast.api import create_app
from rotse_weather_mast.config import Settings
from rotse_weather_mast.poller import Poller
from rotse_weather_mast.sensors import Sensor, SkipReading, build_sensors


class Scripted(Sensor):
    driver = "scripted"

    def __init__(self, name: str, values: dict[str, float]) -> None:
        super().__init__(name)
        self.values = values
        self.error: Exception | None = None

    def read(self) -> dict[str, float]:
        if self.error is not None:
            raise self.error
        return dict(self.values)


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0  # 2027-01-15T08:00:00Z

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def rig():
    clock = Clock()
    settings = Settings.load(None)
    sht = Scripted("sht45", {"temp_c": 20.0, "rh_pct": 50.0})
    bme = Scripted("bme280", {"temp_c": 20.5, "rh_pct": 48.0, "pressure_hpa": 820.0})
    poller = Poller(settings, [sht, bme], clock=clock)
    client = TestClient(create_app(poller, mode="test"))
    return clock, sht, bme, poller, client


def poll(poller: Poller) -> None:
    asyncio.run(poller.poll_once())


def test_defaults_valid():
    s = Settings.load(None)
    assert set(s.sensors) == {"sht45", "bme280"}
    assert s.api.port == 8082


def test_yaml_and_env(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("poll_interval_s: 20\nmax_age_s: 90\nsensors:\n  sht45: {type: sht45, address: 0x45}\n")
    monkeypatch.setenv("MAST_API__PORT", "9000")
    s = Settings.load(cfg)
    assert s.poll_interval_s == 20
    assert s.sensors["sht45"].address == 0x45
    assert list(s.sensors) == ["sht45"]
    assert s.api.port == 9000  # env beats YAML


def test_unknown_key_rejected(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pol_interval_s: 5\n")
    with pytest.raises(ValidationError):
        Settings.load(cfg)


def test_sensors_endpoint(rig):
    clock, sht, bme, poller, client = rig
    poll(poller)
    clock.t += 5
    body = client.get("/api/v1/sensors").json()
    s = body["sensors"]["sht45"]
    assert s["ok"] is True and s["age_s"] == 5.0
    assert s["dewpoint_c"] == pytest.approx(9.26, abs=0.05)
    assert s["time"] == "2027-01-15T08:00:00Z"


def test_failure_keeps_last_values(rig):
    clock, sht, bme, poller, client = rig
    poll(poller)
    sht.error = OSError(121, "Remote I/O error")
    clock.t += 10
    poll(poller)
    s = client.get("/api/v1/sensors").json()["sensors"]["sht45"]
    assert s["ok"] is False and "Remote I/O" in s["error"]
    assert s["temp_c"] == 20.0 and s["age_s"] == 10.0
    assert poller.state["sht45"].failures == 1


def test_skip_is_not_an_error(rig):
    clock, sht, bme, poller, client = rig
    poll(poller)
    sht.error = SkipReading("heater")
    poll(poller)
    assert poller.state["sht45"].ok and poller.state["sht45"].skipped == 1


def test_pyobs_weather_sht45(rig):
    clock, sht, bme, poller, client = rig
    assert client.get("/api/v1/pyobs-weather/sht45").status_code == 503  # nothing read yet
    poll(poller)
    r = client.get("/api/v1/pyobs-weather/sht45")
    assert r.status_code == 200
    doc = r.json()
    assert doc == {"time": "2027-01-15T08:00:00Z", "temp": 20.0, "humid": 50.0,
                   "dewpoint": pytest.approx(9.26, abs=0.05)}
    assert all(isinstance(doc[k], float) for k in ("temp", "humid", "dewpoint"))


def test_sensors_are_independent(rig):
    clock, sht, bme, poller, client = rig
    poll(poller)
    bme.error = OSError("gone")
    poll(poller)
    r = client.get("/api/v1/pyobs-weather/bme280")
    assert r.status_code == 503 and "gone" in r.json()["error"]
    assert client.get("/api/v1/pyobs-weather/sht45").status_code == 200


def test_pyobs_weather_stale(rig):
    clock, sht, bme, poller, client = rig
    poll(poller)
    clock.t += poller.settings.max_age_s + 1
    assert client.get("/api/v1/pyobs-weather/sht45").status_code == 503


def test_pyobs_weather_per_sensor(rig):
    clock, sht, bme, poller, client = rig
    poll(poller)
    doc = client.get("/api/v1/pyobs-weather/bme280").json()
    assert set(doc) == {"time", "temp", "humid", "dewpoint", "press"}
    assert client.get("/api/v1/pyobs-weather/nope").status_code == 404


def test_health(rig):
    clock, sht, bme, poller, client = rig
    assert client.get("/api/v1/health").status_code == 503  # loop not started
    poller.heartbeat_ts = clock.t
    body = client.get("/api/v1/health").json()
    assert body["ok"] is True and body["sensors"]["sht45"]["driver"] == "scripted"


def test_sim_end_to_end():
    settings = Settings.load(None)
    poller = Poller(settings, build_sensors(settings, sim=True))
    poll(poller)
    client = TestClient(create_app(poller, mode="sim"))
    assert 700 < client.get("/api/v1/pyobs-weather/bme280").json()["press"] < 900
    assert -10 < client.get("/api/v1/pyobs-weather/sht45").json()["temp"] < 45


def test_build_real_sensors_with_fake_bus():
    from test_drivers import FakeBME280Bus, FakeSHT45Bus

    class Bus(FakeSHT45Bus, FakeBME280Bus):
        def __init__(self) -> None:
            FakeSHT45Bus.__init__(self)
            FakeBME280Bus.__init__(self)

    settings = Settings.load(None)
    sensors = build_sensors(settings, sim=False, bus=Bus())
    assert [s.driver for s in sensors] == ["sht45", "bme280"]
