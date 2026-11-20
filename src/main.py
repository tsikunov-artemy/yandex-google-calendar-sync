"""
Calendar Sync — bidirectional Yandex Calendar ↔ Google Calendar sync service.

Usage:
    python -m src.main          # Run as daemon with adaptive scheduling
    python -m src.main --once   # Run once and exit
"""

from __future__ import annotations

import argparse
import signal
import time

import structlog

from src.config import get_settings
from src.sync.engine import SyncEngine
from src.sync.scheduler import compute_next_sync_delay
from src.utils.logging import setup_logging

log = structlog.get_logger()

# Graceful shutdown
_running = True


def _signal_handler(signum, frame):
    global _running
    log.info("shutdown.signal_received", signal=signum)
    _running = False


def run_sync_safe(engine: SyncEngine) -> list:
    """Run one sync cycle with error handling. Returns fetched events for scheduler."""
    try:
        engine.run_sync()
        return engine.last_fetched_events
    except Exception as e:
        log.error("sync.cycle_failed", error=str(e))
        return []


def main() -> None:
    parser = argparse.ArgumentParser(description="Calendar Sync: Yandex ↔ Google")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file)

    log.info(
        "startup",
        direction=settings.sync_direction.value,
        conflict_strategy=settings.conflict_strategy.value,
    )

    engine = SyncEngine(settings)

    try:
        engine.connect()

        if args.once:
            log.info("mode.once")
            engine.run_sync()
            return

        # Daemon mode with adaptive scheduling
        log.info("mode.daemon.adaptive")
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        while _running:
            # Run sync and get events for scheduling decisions
            events = run_sync_safe(engine)

            # Compute adaptive delay based on upcoming events
            delay = compute_next_sync_delay(
                events,
                work_start_hour=settings.work_hours_start,
                work_end_hour=settings.work_hours_end,
            )
            log.info("scheduler.next_sync", delay_seconds=delay, delay_minutes=round(delay / 60, 1))

            # Sleep in small increments so we can respond to SIGTERM quickly
            elapsed = 0
            while _running and elapsed < delay:
                time.sleep(min(10, delay - elapsed))
                elapsed += 10

        log.info("shutdown.graceful")

    except KeyboardInterrupt:
        log.info("shutdown.keyboard_interrupt")
    finally:
        engine.close()


if __name__ == "__main__":
    main()
