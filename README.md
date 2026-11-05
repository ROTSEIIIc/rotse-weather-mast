# rotse-weather-mast

Service for the **weather mast Pi 5** at ROTSE-IIIc (H.E.S.S. site). It reads an **SHT45**
(T/RH) and a **BME280** (T/RH/pressure) over I²C, keeps the latest values in memory, and
serves them as JSON for the observatory server's **pyobs-weather** to poll. No UI and no
database. indi-allsky runs on the same Pi for the allsky camera; this service is
independent of it.

Same pattern as `~/rotse-weather-pi` (allsky-station): asyncio, `/api/v1/...` JSON with
`age_s`/`ok`/`error`, `--sim`, config = defaults < YAML < env, systemd unit with watchdog.

```bash
uv sync
uv run rotse-weather-mast --sim            # http://localhost:8082/api/v1/sensors
uv run pytest
```

## API (port 8082)

| Path | What |
|---|---|
| `GET /api/v1/sensors` | All sensors: values, `time` of the reading, `age_s`, `ok`, `error` |
| `GET /api/v1/health` | Loop heartbeat, uptime, CPU temp, per-sensor driver status, error counters. 503 if the loop is stuck |
| `GET /api/v1/pyobs-weather/{sensor}` | Flat document for pyobs-weather, one per sensor (`sht45`, `bme280`) |
| `GET /docs` | OpenAPI page |

```json
GET /api/v1/sensors
{"station": "weather-mast", "time": "2026-11-05T21:14:10Z",
 "sensors": {
   "sht45":  {"temp_c": 14.21, "rh_pct": 38.5, "dewpoint_c": 0.15,
              "time": "2026-11-05T21:14:03Z", "age_s": 7.0, "ok": true},
   "bme280": {"temp_c": 14.73, "rh_pct": 37.1, "pressure_hpa": 821.36, "dewpoint_c": 0.08,
              "time": "2026-11-05T21:14:03Z", "age_s": 7.0, "ok": true}}}

GET /api/v1/pyobs-weather/sht45
{"time": "2026-11-05T21:14:03Z", "temp": 14.21, "humid": 38.5, "dewpoint": 0.15}

GET /api/v1/pyobs-weather/bme280
{"time": "2026-11-05T21:14:03Z", "temp": 14.73, "humid": 37.1, "press": 821.36, "dewpoint": 0.08}
```

Sensor semantics:

* `age_s` is seconds since the last **successful** read. A failed read keeps the old values,
  sets `ok: false` and `error`, and `age_s` keeps growing.
* `ok` is also false once a reading is older than `max_age_s` (default 60 s).
* `dewpoint_c` is computed from each sensor's own T/RH (Magnus, Sonntag constants).

### The pyobs-weather documents

pyobs-weather 2.2.5's `JSON` station (`weather/stations/json.py`) reads `weather[field]`
for every configured field and parses `weather["time"]`. It skips the update on any status
other than 200. So these endpoints:

* are flat, with every key always present (a missing key would raise mid-update),
* use the **time of the reading** for `time`, not the time of the request,
* always send floats (InfluxDB rejects a field whose type changes),
* return **503** when that sensor's reading is failed or stale.

The SHT45 and BME280 are **separate pyobs-weather stations**, so a dead BME280 doesn't take
the SHT45 values down with it, and each has its own `time`. pyobs-weather's average station
then combines them per sensor type. Keys are pyobs-weather's built-in sensor type codes
(`temp`, `humid`, `dewpoint`, `press`), so the station config needs no names or units.

pyobs-weather stations (admin → Stations, class `pyobs_weather.weather.stations.JSON`):

```json
// station "mast-sht45"
{"url": "http://weather-mast.local:8082/api/v1/pyobs-weather/sht45",
 "fields": {"temp": {"code": "temp"}, "humid": {"code": "humid"}, "dewpoint": {"code": "dewpoint"}}}

// station "mast-bme280"
{"url": "http://weather-mast.local:8082/api/v1/pyobs-weather/bme280",
 "fields": {"press": {"code": "press"}, "temp": {"code": "temp"}, "humid": {"code": "humid"},
            "dewpoint": {"code": "dewpoint"}}}
```

pyobs-weather's `average` station averages the latest value of each sensor type over all
active stations, weighted per station. To keep the BME280's T/RH (which tends to read a bit
warm) out of the average but still record it, untick "use in average" on those sensors in
the admin. To not record it at all, leave them out of that station's `fields`.

## Configuration

Everything is optional. See [`config.example.yaml`](config.example.yaml). Environment
overrides use `MAST_` plus `__` for nesting (`MAST_API__PORT=9000`). Unknown keys are an
error, so typos don't pass silently.

## The hardware

| Sensor | Address | Notes |
|---|---|---|
| SHT45 | 0x44 | Main T/RH. Built-in heater pulses after RH ≥ 95 % for 10 min; the next reading is discarded |
| BME280 | 0x76 or 0x77 | Pressure (plus a second T/RH). Forced mode, x1 oversampling, so self-heating is negligible. Checks the chip ID: a BMP280 (no humidity) is rejected with a clear error |

Drivers use `smbus2` directly (no Adafruit Blinka, which is awkward on the Pi 5). The SHT45
driver follows the Sensirion SHT4x datasheet (commands, CRC-8, conversions). The BME280
uses the `RPi.bme280` package (Bosch compensation formulas). Both are tested against the
datasheet examples with fake buses.

**Cable:** keep I²C at 100 kHz (the default) on a long run up the mast, use twisted pairs
(SDA+GND, SCL+GND), and keep the bus short. If it's longer than a couple of metres, an I²C
extender (e.g. P82B715 or an LTC4311 accelerator) is the fix, not a lower poll rate.

**indi-allsky:** don't configure these two sensors in indi-allsky as well. Two processes
reading them collide on the bus and both drive the SHT45 heater. This service owns the I²C
sensors.

## Deploying on the Pi

```bash
sudo raspi-config nonint do_i2c 0           # enable I2C (or dtparam=i2c_arm=on)
i2cdetect -y 1                              # expect 44 and 76/77

sudo useradd --system --groups i2c --home /opt/rotse-weather-mast weather
sudo git clone https://github.com/<you>/rotse-weather-mast /opt/rotse-weather-mast   # or a tag
cd /opt/rotse-weather-mast && sudo uv sync --no-dev
sudo chown -R weather: /opt/rotse-weather-mast

sudo install -D -m 644 config.example.yaml /etc/rotse-weather-mast/config.yaml       # then edit
sudo cp deploy/rotse-weather-mast.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now rotse-weather-mast
journalctl -u rotse-weather-mast -f
curl -s localhost:8082/api/v1/sensors | python3 -m json.tool
```

## Adding a sensor

Write a driver in `sensors/` (a `Sensor` subclass whose `read()` returns `{field: float}`),
add a simulator in `sensors/sim.py`, then register both in `sensors/registry.py` and the
`type` literal in `config.py`. To publish its fields to pyobs-weather, map them in
`pyobs_weather.field_codes`.
