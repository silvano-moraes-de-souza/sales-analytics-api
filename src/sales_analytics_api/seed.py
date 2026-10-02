"""Build a lakehouse for the API to serve, with the mini-lakehouse of day 04.

The day-04 pipeline merges silver one day at a time, which is the point of that
project and slow for seeding. Here every drop is ingested into bronze and silver
is rebuilt once; the test suite of day 04 proves both paths give the same silver.
"""

from __future__ import annotations

from pathlib import Path

from mini_lakehouse import bronze, gold, landing, silver
from shopflow_datagen import GenConfig, write


def build(lake: Path, scale: float = 0.1, config: GenConfig | None = None) -> dict:
    source = lake / "source"
    write(config or GenConfig(scale=scale), source)
    landing.build(source, lake / "landing")
    for day in landing.arrival_days(lake / "landing"):
        bronze.ingest(lake / "landing", lake / "bronze", day)
    silver.rebuild(lake / "bronze", lake / "silver")
    gold.build(lake / "silver", lake / "gold")
    return gold.reconcile(source, lake / "silver", lake / "gold")
