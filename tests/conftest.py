from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from shopflow_datagen import GenConfig

from sales_analytics_api.app import create_app
from sales_analytics_api.seed import build


@pytest.fixture(scope="session")
def lake(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("lake")
    cfg = GenConfig(scale=0.05, start=date(2025, 5, 1), end=date(2025, 7, 31), chunk_size=2_000)
    report = build(path, config=cfg)
    assert report["revenue_diff_cents"] == 0 and report["orders_missing_or_different"] == 0
    return path


@pytest.fixture(scope="session")
def client(lake) -> TestClient:
    return TestClient(create_app(lake))
