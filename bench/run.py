"""Latency and throughput of the running API, and what the gold layer saves.

    uv run python -m bench.run

Seeds a scale 10 lakehouse (not timed), starts the API with uvicorn in another
process and drives it over HTTP on localhost. Writes results/*.json and charts.
"""

from __future__ import annotations

import http.client
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

import httpx
import matplotlib

from bench.harness import CaseResult, save
from sales_analytics_api import queries
from sales_analytics_api.seed import build
from sales_analytics_api.store import Store

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SCALE = float(os.environ.get("BENCH_SCALE", "10"))
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
SECONDS = 8
CONCURRENCY = [1, 8, 32]
YEAR = "start=2025-01-01&end=2025-12-31"
SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
COLORS = ["#2a78d6", "#c2410c", "#52514e"]


def endpoints(store: Store) -> dict:
    customers = [r["customer_id"] for r in store.query(
        "SELECT customer_id FROM customers USING SAMPLE 500 ROWS (reservoir, 42)")]  # fmt: skip
    orders = [r["order_id"] for r in store.query(
        "SELECT order_id FROM orders USING SAMPLE 500 ROWS (reservoir, 42)")]  # fmt: skip
    rng = random.Random(42)
    return {
        "summary by month (gold)": lambda: f"/v1/sales/summary?{YEAR}&group_by=month",
        "daily sales page (gold)": lambda: f"/v1/sales/daily?{YEAR}&limit=100",
        "top categories (gold)": lambda: f"/v1/categories/top?month=2025-{rng.randint(1, 12):02d}",
        "customer orders (silver)": lambda: f"/v1/customers/{rng.choice(customers)}/orders",
        "order detail (silver)": lambda: f"/v1/orders/{rng.choice(orders)}",
    }


def load(path_fn, concurrency: int, headers: dict | None = None) -> dict:
    """``concurrency`` threads, each with one keep-alive connection, for SECONDS."""
    results: list[list[float]] = [[] for _ in range(concurrency)]
    errors = [0] * concurrency
    start_gate = threading.Event()
    deadline = [0.0]

    def worker(i: int) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
        conn.request("GET", "/health")
        conn.getresponse().read()
        start_gate.wait()
        mine = results[i]
        while time.perf_counter() < deadline[0]:
            t0 = time.perf_counter()
            conn.request("GET", path_fn(), headers=headers or {})
            r = conn.getresponse()
            r.read()
            mine.append(time.perf_counter() - t0)
            errors[i] += r.status >= 400
        conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(concurrency)]
    for t in threads:
        t.start()
    time.sleep(0.5)  # every worker connected and waiting
    start = time.perf_counter()
    deadline[0] = start + SECONDS
    start_gate.set()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start
    latencies = sorted(x for r in results for x in r)
    pick = lambda q: latencies[min(len(latencies) - 1, int(q * len(latencies)))]  # noqa: E731
    return {"requests": len(latencies), "errors": sum(errors), "rps": len(latencies) / elapsed,
            "p50_ms": pick(0.50) * 1000, "p95_ms": pick(0.95) * 1000, "p99_ms": pick(0.99) * 1000}  # fmt: skip


def http_bench(store: Store, where: str) -> tuple[Path, dict]:
    cases, table = [], {}
    eps = endpoints(store)
    for name, fn in eps.items():
        for c in CONCURRENCY:
            r = load(fn, c)
            assert r["errors"] == 0, (name, c, r)
            table[(name, c)] = r
            cases.append(CaseResult(f"{name} · {c} clients", {"endpoint": name, "clients": c}, 1,
                                    [r["p50_ms"] / 1000], [0.0], {k: round(v, 3) for k, v in r.items()}))  # fmt: skip
            print(f"{name:28s} c={c:<3d} p50 {r['p50_ms']:7.2f} ms  p95 {r['p95_ms']:7.2f} ms  "
                  f"{r['rps']:7.0f} req/s", flush=True)  # fmt: skip
    # Revalidation: same URL with If-None-Match, answered with 304 before any query runs.
    url = f"/v1/sales/summary?{YEAR}&group_by=month"
    etag = httpx.get(BASE + url).headers["etag"]
    r = load(lambda: url, 8, {"If-None-Match": etag})
    table[("summary revalidated (304)", 8)] = r
    cases.append(CaseResult("summary revalidated (304) · 8 clients",
                            {"endpoint": "summary revalidated (304)", "clients": 8}, 1,
                            [r["p50_ms"] / 1000], [0.0], {k: round(v, 3) for k, v in r.items()}))  # fmt: skip
    print(
        f"{'summary revalidated (304)':28s} c=8   p50 {r['p50_ms']:7.2f} ms  {r['rps']:7.0f} req/s"
    )
    path = save("http_latency", cases, notes=(
        f"ShopFlow scale {SCALE:g} lakehouse. uvicorn, one process, on localhost; load from one "
        f"thread per client with a keep-alive http.client connection, {SECONDS} s per case. "
        f"Latency is measured at the client. wall_s holds the p50. {where}."))  # fmt: skip
    return path, table


def timed(fn, label: str, runs: int, params: dict | None = None) -> CaseResult:
    """Plain timing loop. The shared harness samples memory in a thread on every
    run, which costs about 0.6 ms: too much noise for sub-millisecond queries."""
    for _ in range(5):
        fn()
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return CaseResult(label, params or {}, runs, times, [0.0])


def gold_vs_silver(store: Store, where: str) -> Path:
    args = (date(2025, 1, 1), date(2025, 12, 31), "month")
    assert queries.summary(store, *args) == queries.summary_from_silver(store, *args)
    cases = [
        timed(lambda: queries.summary(store, *args), "from gold (pre-aggregated)", 100,
              {"rows_scanned": store.scalar("SELECT count(*) FROM daily_sales")}),
        timed(lambda: queries.summary_from_silver(store, *args),
              "from silver (aggregate on every call)", 30,
              {"rows_scanned": store.scalar("SELECT count(*) FROM orders")}),
    ]  # fmt: skip
    for c in cases:
        print(f"{c.label}: {c.median_s * 1000:.2f} ms")
    return save("gold_vs_silver", cases, notes=(
        "Monthly summary of 2025, same result (asserted), in-process, no HTTP. "
        f"Gold is loaded in memory at startup; silver is Parquet on disk. {where}."))  # fmt: skip


def bound_vs_literal(store: Store, where: str) -> Path:
    """What binding parameters costs in DuckDB's Python client, on the summary query."""
    start, end = date(2025, 1, 1), date(2025, 12, 31)
    sql = ("SELECT strftime(day, '%Y-%m') AS key, sum(orders) AS orders, sum(revenue_cents) AS "
           "revenue_cents FROM daily_sales WHERE day BETWEEN {a} AND {b} GROUP BY 1 ORDER BY 1")  # fmt: skip
    bound = lambda: store.query(sql.format(a="?", b="?"), [start, end])  # noqa: E731
    literal = lambda: store.query(sql.format(a=queries.lit(start), b=queries.lit(end)))  # noqa: E731
    assert bound() == literal()
    cases = [
        timed(lambda: store.query("SELECT 1 AS x"), "SELECT 1", 300),
        timed(lambda: store.query("SELECT ? AS x", [1]), "SELECT ? (one bound parameter)", 300),
        timed(literal, "summary, typed literals", 300),
        timed(bound, "summary, two bound parameters", 300),
    ]  # fmt: skip
    for c in cases:
        print(f"{c.label}: {c.median_s * 1000:.3f} ms")
    return save("bound_vs_literal", cases, notes=(
        f"In-process, median of 300 runs each, same result asserted. {where}."))  # fmt: skip


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
    ax.tick_params(axis="x", colors=MUTED, labelsize=8)
    ax.tick_params(axis="y", length=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)


def charts(table: dict) -> None:
    names = list(dict.fromkeys(n for n, _ in table))
    fig, ax = plt.subplots(figsize=(8, 0.75 * len(names) + 1.5), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    y = range(len(names))
    for i, (key, label) in enumerate((("p50_ms", "p50"), ("p95_ms", "p95"), ("p99_ms", "p99"))):
        vals = [table[(n, 8)][key] for n in names][::-1]
        ax.barh(
            [j + 0.26 - 0.26 * i for j in y],
            vals,
            height=0.24,
            color=COLORS[i],
            label=label,
            zorder=2,
        )
        for j, v in enumerate(vals):
            ax.text(v * 1.08, j + 0.26 - 0.26 * i, f"{v:.3g}", va="center", fontsize=7, color=INK)
    ax.set_xscale("log")
    ax.set_yticks(list(y), names[::-1], fontsize=8.5, color=INK)
    ax.set_xlabel("Latency with 8 concurrent clients, ms (log scale)", color=MUTED, fontsize=9)
    ax.legend(loc="lower right", fontsize=8, frameon=False)
    ax.set_title(
        f"Latency by endpoint, ShopFlow scale {SCALE:g}", loc="left", color=INK, fontsize=11, pad=12
    )
    _style(ax)
    fig.tight_layout()
    fig.savefig("docs/assets/latency_by_endpoint.png", facecolor=SURFACE)
    plt.close(fig)

    names = [n for n in names if (n, 1) in table]
    fig, ax = plt.subplots(figsize=(8, 0.75 * len(names) + 1.5), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    for i, c in enumerate(CONCURRENCY):
        vals = [table[(n, c)]["rps"] for n in names][::-1]
        ax.barh([j + 0.26 - 0.26 * i for j in range(len(names))], vals, height=0.24, color=COLORS[i],
                label=f"{c} client{'s' if c > 1 else ''}", zorder=2)  # fmt: skip
        for j, v in enumerate(vals):
            ax.text(
                v * 1.01 + 5, j + 0.26 - 0.26 * i, f"{v:,.0f}", va="center", fontsize=7, color=INK
            )
    ax.set_yticks(list(range(len(names))), names[::-1], fontsize=8.5, color=INK)
    ax.set_xlabel("Requests per second", color=MUTED, fontsize=9)
    ax.legend(loc="lower right", fontsize=8, frameon=False)
    ax.set_title(
        f"Throughput by concurrency, ShopFlow scale {SCALE:g}",
        loc="left",
        color=INK,
        fontsize=11,
        pad=12,
    )
    _style(ax)
    fig.tight_layout()
    fig.savefig("docs/assets/throughput_by_concurrency.png", facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    lake = Path(tempfile.mkdtemp(prefix=f"sales-api-sf{SCALE:g}-"))
    report = build(lake, SCALE)
    assert report["revenue_diff_cents"] == 0 and report["orders_missing_or_different"] == 0, report
    store = Store(lake)
    where = f"Local disk, Python {sys.version.split()[0]}"
    print(gold_vs_silver(store, where))
    print(bound_vs_literal(store, where))
    server = subprocess.Popen(
        [sys.executable, "-m", "sales_analytics_api.cli", "serve", "--lake", str(lake), "--port", str(PORT)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    try:
        for _ in range(120):
            try:
                if httpx.get(f"{BASE}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        path, table = http_bench(store, where)
        print(path)
        charts(table)
    finally:
        server.terminate()
        server.wait(timeout=20)
        store.close()


if __name__ == "__main__":
    main()
