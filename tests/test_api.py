from datetime import date

import duckdb
import pytest

from sales_analytics_api import queries
from sales_analytics_api.pagination import decode, encode

RANGE = {"start": "2025-05-01", "end": "2025-07-31"}
LOCAL_DAY = "CAST(ordered_at - INTERVAL 3 HOUR AS DATE)"


def _source(lake, sql: str):
    orders = f"'{(lake / 'source/orders').as_posix()}/*.parquet'"
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    try:
        return con.execute(sql.format(orders=orders)).fetchall()
    finally:
        con.close()


def test_health_reports_the_data_version(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and len(body["data_version"]) == 16 and body["orders"] > 0


def test_daily_sales_pages_cover_every_row_exactly_once(client):
    seen, cursor, pages = [], None, 0
    while True:
        params = {**RANGE, "limit": 37, **({"cursor": cursor} if cursor else {})}
        body = client.get("/v1/sales/daily", params=params).json()
        seen += [(r["day"], r["channel"]) for r in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    everything = client.get("/v1/sales/daily", params={**RANGE, "limit": 500}).json()["items"]
    assert pages > 3
    assert seen == [(r["day"], r["channel"]) for r in everything]
    assert len(seen) == len(set(seen)) and seen == sorted(seen)


def test_summary_revenue_matches_the_source_to_the_cent(client, lake):
    body = client.get("/v1/sales/summary", params={**RANGE, "group_by": "month"}).json()
    truth = _source(
        lake,
        "SELECT sum(total_cents) FROM {orders} WHERE status IN ('shipped', 'delivered') "
        f"AND {LOCAL_DAY} BETWEEN DATE '2025-05-01' AND DATE '2025-07-31'",
    )[0][0]
    assert body["total_revenue_cents"] == truth
    assert [r["key"] for r in body["rows"]] == ["2025-05", "2025-06", "2025-07"]


def test_gold_and_silver_give_the_same_summary(client):
    store = client.app.state.store
    args = (date(2025, 5, 1), date(2025, 7, 31))
    for group in ("channel", "month"):
        gold = queries.summary(store, *args, group)
        assert gold == queries.summary_from_silver(store, *args, group)


def test_channel_filter_only_returns_that_channel(client):
    channel = client.get("/v1/sales/daily", params=RANGE).json()["items"][0]["channel"]
    items = client.get("/v1/sales/daily", params={**RANGE, "channel": channel}).json()["items"]
    assert items and {r["channel"] for r in items} == {channel}


def test_top_categories_are_ranked_by_revenue(client):
    body = client.get("/v1/categories/top", params={"month": "2025-06", "limit": 5}).json()
    revenue = [r["revenue_cents"] for r in body["rows"]]
    assert len(revenue) == 5 and revenue == sorted(revenue, reverse=True)


def test_customer_orders_newest_first_and_paged(client, lake):
    cid, n = _source(
        lake, "SELECT customer_id, count(*) FROM {orders} GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1"
    )[0]
    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = client.get(f"/v1/customers/{cid}/orders", params=params).json()
        seen += body["items"]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == n == len({o["order_id"] for o in seen})
    times = [o["ordered_at"] for o in seen]
    assert times == sorted(times, reverse=True)


def test_order_detail_items_add_up(client, lake):
    oid = _source(lake, "SELECT order_id FROM {orders} ORDER BY order_id LIMIT 1 OFFSET 50")[0][0]
    body = client.get(f"/v1/orders/{oid}").json()
    assert body["order_id"] == oid and body["items"]
    assert sum(i["line_total_cents"] for i in body["items"]) == body["subtotal_cents"]
    total = body["subtotal_cents"] - body["discount_cents"] + body["shipping_cents"]
    assert total == body["total_cents"]


def test_errors_are_problem_json(client):
    cases = [
        ("/v1/orders/999999999", {}, 404),
        ("/v1/customers/999999999/orders", {}, 404),
        ("/v1/sales/daily", {"start": "2025-07-01", "end": "2025-06-01"}, 400),
        ("/v1/sales/daily", {"start": "2020-01-01", "end": "2025-06-01"}, 400),
        ("/v1/sales/daily", {**RANGE, "cursor": "not-a-cursor"}, 400),
        ("/v1/sales/daily", {**RANGE, "limit": 0}, 422),
        ("/v1/sales/daily", {"start": "yesterday", "end": "2025-06-01"}, 422),
        ("/v1/categories/top", {"month": "2025-13"}, 422),
        ("/v1/sales/summary", {**RANGE, "group_by": "color"}, 422),
    ]
    for path, params, status in cases:
        r = client.get(path, params=params)
        assert r.status_code == status, (path, params, r.text)
        assert r.headers["content-type"].startswith("application/problem+json")
        assert r.json()["status"] == status and r.json()["title"]


def test_etag_gives_304_on_revalidation(client):
    first = client.get("/v1/sales/summary", params=RANGE)
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "public, max-age=60"
    again = client.get("/v1/sales/summary", params=RANGE, headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
    other = client.get("/v1/sales/summary", params={**RANGE, "group_by": "month"})
    assert other.headers["etag"] != etag


def test_sql_injection_attempt_is_just_a_value(client):
    r = client.get("/v1/sales/daily", params={**RANGE, "channel": "web' OR '1'='1"})
    assert r.status_code == 200 and r.json()["items"] == []


def test_openapi_documents_every_route(client):
    spec = client.get("/openapi.json").json()
    assert set(spec["paths"]) == {
        "/health",
        "/v1/sales/daily",
        "/v1/sales/summary",
        "/v1/categories/top",
        "/v1/customers/{customer_id}/orders",
        "/v1/orders/{order_id}",
    }
    assert "Problem" in spec["components"]["schemas"]


def test_cursor_round_trip():
    assert decode(encode(["2025-06-01", "web"]), 2) == ["2025-06-01", "web"]


def test_lit_only_renders_typed_values():
    assert queries.lit(42) == "42"
    assert queries.lit(date(2025, 6, 1)) == "DATE '2025-06-01'"
    for bad in ("2025-06-01", "1 OR 1=1", True, 1.5, None):
        with pytest.raises(TypeError):
            queries.lit(bad)


def test_hostile_cursors_are_rejected_or_treated_as_values(client, lake):
    before = client.get("/health").json()
    drop = encode(["2025-06-01'); DROP TABLE daily_sales; --", "web"])
    assert client.get("/v1/sales/daily", params={**RANGE, "cursor": drop}).status_code == 400
    quoted = encode(["2025-06-01", "web' OR '1'='1"])
    ok = client.get("/v1/sales/daily", params={**RANGE, "cursor": quoted})
    assert ok.status_code == 200
    assert all(
        (r["day"], r["channel"]) > ("2025-06-01", "web' OR '1'='1") for r in ok.json()["items"]
    )
    cid = _source(lake, "SELECT customer_id FROM {orders} LIMIT 1")[0][0]
    for bad in (["2025-06-01T00:00:00+00:00", "1 OR 1=1"], ["now()", 1], [None, None]):
        r = client.get(f"/v1/customers/{cid}/orders", params={"cursor": encode(bad)})
        assert r.status_code == 400, bad
    assert client.get("/health").json() == before
