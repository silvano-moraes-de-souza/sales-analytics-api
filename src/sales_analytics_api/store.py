"""Read-only access to the lakehouse through DuckDB.

Gold tables are small, so they are loaded into memory once at startup. Silver
stays on disk as Parquet, read through views with Hive partitioning.

One DuckDB connection is shared; every request takes its own cursor, which is
how DuckDB expects a connection to be used from several threads.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb


class LakeNotFoundError(RuntimeError):
    pass


class Store:
    def __init__(self, lake: Path) -> None:
        gold, silver = lake / "gold", lake / "silver"
        if not (gold / "daily_sales.parquet").exists():
            raise LakeNotFoundError(f"no gold layer under {lake}; run `sales-api seed` first")
        self.lake = lake
        self._con = duckdb.connect()
        # GLOBAL, not session: each request runs on its own cursor, and a cursor
        # does not inherit session settings. Without this, day boundaries would
        # follow the time zone of whatever machine the API runs on.
        self._con.execute("SET GLOBAL TimeZone = 'UTC'")
        for name in ("daily_sales", "category_monthly"):
            self._con.execute(
                f"CREATE TABLE {name} AS SELECT * FROM '{(gold / f'{name}.parquet').as_posix()}'"
            )
        for name in ("orders", "order_items"):
            self._con.execute(
                f"CREATE VIEW {name} AS SELECT * FROM read_parquet("
                f"'{(silver / name).as_posix()}/*/*.parquet', hive_partitioning = true)"
            )
        for name in ("customers", "products"):
            self._con.execute(
                f"CREATE VIEW {name} AS SELECT * FROM '{(silver / name).as_posix()}/data.parquet'"
            )
        self.version = self._version(gold)

    @staticmethod
    def _version(gold: Path) -> str:
        """Changes whenever a gold file is rewritten; used for ETags."""
        h = hashlib.sha256()
        for f in sorted(gold.glob("*.parquet")):
            st = f.stat()
            h.update(f"{f.name}:{st.st_size}:{st.st_mtime_ns}".encode())
        return h.hexdigest()[:16]

    def query(self, sql: str, params: list | None = None) -> list[dict]:
        cur = self._con.cursor()
        try:
            cur.execute(sql, params or [])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
        finally:
            cur.close()

    def scalar(self, sql: str, params: list | None = None):
        cur = self._con.cursor()
        try:
            return cur.execute(sql, params or []).fetchone()[0]
        finally:
            cur.close()

    def close(self) -> None:
        self._con.close()
