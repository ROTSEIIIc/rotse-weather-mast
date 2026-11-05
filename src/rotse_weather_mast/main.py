"""Entry point: ``rotse-weather-mast [--sim] [--config config.yaml]``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from . import sdnotify
from .api import create_app
from .config import Settings
from .poller import Poller
from .sensors import build_sensors

log = logging.getLogger("rotse_weather_mast")


async def _watchdog(poller: Poller, interval_s: float) -> None:
    """Ping systemd only while the poll loop is alive, so a hung loop gets restarted."""
    while True:
        if poller.loop_healthy():
            sdnotify.notify("WATCHDOG=1")
        await asyncio.sleep(interval_s)


def build_app(settings: Settings, sim: bool) -> FastAPI:
    poller = Poller(settings, build_sensors(settings, sim=sim))

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        tasks = [asyncio.create_task(poller.run(), name="poller")]
        wd = sdnotify.watchdog_interval_s()
        if wd:
            tasks.append(asyncio.create_task(_watchdog(poller, wd), name="watchdog"))
        sdnotify.notify("READY=1")
        try:
            yield
        finally:
            sdnotify.notify("STOPPING=1")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            poller.close()

    return create_app(poller, mode="sim" if sim else "hardware", lifespan=lifespan)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="ROTSE weather mast: I2C sensors -> JSON for pyobs-weather")
    p.add_argument(
        "--sim",
        action="store_true",
        default=os.environ.get("MAST_SIM") == "1",
        help="simulated sensors (no hardware needed)",
    )
    p.add_argument("--config", type=Path, default=Path("config.yaml"), help="YAML config (optional)")
    p.add_argument("--host", help="override api.host")
    p.add_argument("--port", type=int, help="override api.port")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    settings = Settings.load(args.config)
    log.info(
        "starting station=%s mode=%s config=%s (%s) sensors=%s",
        settings.station,
        "sim" if args.sim else "hardware",
        args.config,
        "loaded" if args.config.exists() else "not found, using defaults",
        ", ".join(f"{n}:{c.type}" for n, c in settings.sensors.items()),
    )
    uvicorn.run(
        build_app(settings, sim=args.sim),
        host=args.host or settings.api.host,
        port=args.port or settings.api.port,
        log_level=args.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
