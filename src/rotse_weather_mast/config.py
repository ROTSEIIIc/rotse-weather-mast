"""Configuration: defaults < YAML file < environment variables.

Environment overrides use the prefix ``MAST_`` and ``__`` for nesting, e.g.
``MAST_POLL_INTERVAL_S=5`` or ``MAST_API__PORT=9000``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HeaterConfig(_Section):
    """SHT45 built-in heater, to drive off condensation after long periods near 100 % RH."""

    enabled: bool = True
    rh_threshold_pct: float = Field(95.0, ge=50, le=100)
    hold_s: float = Field(600.0, ge=0, description="RH must stay at/above threshold this long before a pulse")
    power: Literal["low", "medium", "high"] = "medium"  # 20 / 110 / 200 mW
    pulse_s: Literal[0.1, 1.0] = 1.0


class SensorConfig(_Section):
    """One I2C sensor. ``type`` picks the driver in ``sensors/registry.py``."""

    type: Literal["sht45", "bme280", "sim_sht45", "sim_bme280"]
    address: int | None = Field(None, description="7-bit I2C address; driver default if omitted")


class SimConfig(_Section):
    seed: int = 0
    dropout_prob: float = Field(0.0, ge=0, le=1, description="Chance that a simulated read raises an I2C error")
    lon: float = Field(16.50, description="Longitude for the simulated diurnal cycle (H.E.S.S. site)")


class ApiConfig(_Section):
    host: str = "0.0.0.0"
    port: int = Field(8082, ge=1, le=65535)


# Sensor field -> pyobs-weather sensor type code (pyobs_weather/weather/stations/station.py SENSOR_TYPES).
DEFAULT_FIELD_CODES: dict[str, str] = {
    "temp_c": "temp",
    "rh_pct": "humid",
    "dewpoint_c": "dewpoint",
    "pressure_hpa": "press",
}


class PyobsWeatherConfig(_Section):
    field_codes: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_FIELD_CODES),
        description="Sensor field -> JSON key in /api/v1/pyobs-weather/{sensor}",
    )


def _default_sensors() -> dict[str, SensorConfig]:
    return {
        "sht45": SensorConfig(type="sht45", address=0x44),
        "bme280": SensorConfig(type="bme280", address=0x76),
    }


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAST_", env_nested_delimiter="__", extra="forbid")

    # Set by Settings.load(); pydantic-settings reads the YAML from here.
    _yaml_file: ClassVar[Path | None] = None

    station: str = "weather-mast"
    i2c_bus: int = Field(1, ge=0, description="/dev/i2c-N; the Pi header pins 3/5 are bus 1")
    poll_interval_s: float = Field(10.0, ge=1, le=3600)
    read_timeout_s: float = Field(5.0, gt=0, le=60)
    max_age_s: float = Field(60.0, gt=0, description="Readings older than this are stale (503 for pyobs-weather)")
    heater: HeaterConfig = Field(default_factory=HeaterConfig)
    sensors: dict[str, SensorConfig] = Field(default_factory=_default_sensors)
    pyobs_weather: PyobsWeatherConfig = Field(default_factory=PyobsWeatherConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    sim: SimConfig = Field(default_factory=SimConfig)

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if not self.sensors:
            raise ValueError("at least one sensor must be configured")
        if self.max_age_s < self.poll_interval_s:
            raise ValueError("max_age_s must be >= poll_interval_s, or every reading is stale")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if cls._yaml_file is not None and cls._yaml_file.exists():
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=cls._yaml_file))
        return tuple(sources)

    @classmethod
    def load(cls, path: Path | None, **overrides: Any) -> Settings:
        """Load defaults < ``path`` (if it exists) < environment < ``overrides``."""
        cls._yaml_file = path
        try:
            return cls(**overrides)
        finally:
            cls._yaml_file = None
