"""SQL behind each endpoint.

How values reach the SQL text:

- Free text (a channel name) is always a bound parameter.
- Integers, dates and timestamps are rendered as typed literals by ``lit``, which
  only accepts values that are already ``int``, ``date`` or ``datetime``. Values
  that come from a client-supplied cursor are parsed into those types first.

Binding costs about 1.3 ms per parameter in DuckDB's Python client (measured in
bench/run.py), more than the whole query on a small gold table, so typed values
are not bound. No request text is ever concatenated into SQL.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .pagination import BadCursorError, decode, encode
from .store import Store

LOCAL_DAY = "CAST(ordered_at - INTERVAL 3 HOUR AS DATE)"
EARNING = "('shipped', 'delivered')"
MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


def lit(value: int | date | datetime) -> str:
    """Render a typed value as a SQL literal. Strings are refused on purpose."""
    if isinstance(value, bool) or not isinstance(value, int | date | datetime):
        raise TypeError(f"lit() takes int, date or datetime, got {type(value).__name__}")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, datetime):
        return f"TIMESTAMPTZ '{value.isoformat()}'"
    return f"DATE '{value.isoformat()}'"


def _from_cursor(parse, raw):
    try:
        return parse(raw)
    except (TypeError, ValueError) as exc:
        raise BadCursorError("cursor is not valid for this listing") from exc


def health(store: Store) -> dict:
    return {
        "status": "ok",
        "data_version": store.version,
        "orders": store.scalar("SELECT sum(orders) FROM daily_sales"),
        "days": store.scalar("SELECT count(DISTINCT day) FROM daily_sales"),
    }


def daily_sales(
    store: Store, start: date, end: date, channel: str | None, limit: int, cursor: str | None
) -> dict:
    where, params = [f"day BETWEEN {lit(start)} AND {lit(end)}"], []
    if channel:
        where.append("channel = ?")
        params.append(channel)
    if cursor:
        day, ch = decode(cursor, 2)
        where.append(f"(day, channel) > ({lit(_from_cursor(date.fromisoformat, day))}, ?)")
        params.append(str(ch))
    rows = store.query(
        f"SELECT day, channel, orders, revenue_cents, canceled, returned FROM daily_sales "
        f"WHERE {' AND '.join(where)} ORDER BY day, channel LIMIT {lit(limit + 1)}",
        params,
    )
    more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1] if rows else None
    return {"items": rows, "next_cursor": encode([last["day"], last["channel"]]) if more else None}


SUMMARY_KEY = {"channel": "channel", "month": "strftime(day, '%Y-%m')"}


def summary(store: Store, start: date, end: date, group_by: str) -> dict:
    key = SUMMARY_KEY[group_by]  # from a fixed mapping, never from the request text
    rows = store.query(
        f"SELECT {key} AS key, sum(orders) AS orders, coalesce(sum(revenue_cents), 0) AS "
        f"revenue_cents, sum(canceled) AS canceled, sum(returned) AS returned "
        f"FROM daily_sales WHERE day BETWEEN {lit(start)} AND {lit(end)} GROUP BY 1 ORDER BY 1"
    )
    return {"group_by": group_by, "start": start, "end": end, "rows": rows,
            "total_revenue_cents": sum(r["revenue_cents"] for r in rows)}  # fmt: skip


def summary_from_silver(store: Store, start: date, end: date, group_by: str) -> dict:
    """The same answer computed from silver orders on every call. Not served by the
    API: it exists so the benchmark can show what the gold layer saves."""
    key = {"channel": "channel", "month": f"strftime({LOCAL_DAY}, '%Y-%m')"}[group_by]
    rows = store.query(
        f"SELECT {key} AS key, count(*) AS orders, coalesce(sum(total_cents) FILTER "
        f"(WHERE status IN {EARNING}), 0) AS revenue_cents, "
        f"count(*) FILTER (WHERE status = 'canceled') AS canceled, "
        f"count(*) FILTER (WHERE status = 'returned') AS returned "
        f"FROM orders WHERE {LOCAL_DAY} BETWEEN {lit(start)} AND {lit(end)} GROUP BY 1 ORDER BY 1"
    )
    return {"group_by": group_by, "start": start, "end": end, "rows": rows,
            "total_revenue_cents": sum(r["revenue_cents"] for r in rows)}  # fmt: skip


def top_categories(store: Store, month: str, limit: int) -> dict:
    if not MONTH.fullmatch(month):
        raise ValueError("month must be YYYY-MM")
    rows = store.query(
        f"SELECT category, units, revenue_cents FROM category_monthly WHERE month = '{month}' "
        f"ORDER BY revenue_cents DESC, category LIMIT {lit(limit)}"
    )
    return {"month": month, "rows": rows}


ORDER_COLS = "order_id, ordered_at, status, channel, total_cents"


def customer_orders(store: Store, customer_id: int, limit: int, cursor: str | None) -> dict | None:
    cid = lit(customer_id)
    if not store.scalar(f"SELECT count(*) FROM customers WHERE customer_id = {cid}"):
        return None
    where = [f"customer_id = {cid}"]
    if cursor:
        ts, oid = decode(cursor, 2)
        ts = lit(_from_cursor(datetime.fromisoformat, ts))
        where.append(f"(ordered_at, order_id) < ({ts}, {lit(_from_cursor(int, oid))})")
    rows = store.query(
        f"SELECT {ORDER_COLS} FROM orders WHERE {' AND '.join(where)} "
        f"ORDER BY ordered_at DESC, order_id DESC LIMIT {lit(limit + 1)}"
    )
    more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1] if rows else None
    token = encode([last["ordered_at"].isoformat(), last["order_id"]]) if more else None
    return {"items": rows, "next_cursor": token}


def order(store: Store, order_id: int) -> dict | None:
    oid = lit(order_id)
    found = store.query(
        f"SELECT {ORDER_COLS}, customer_id, subtotal_cents, discount_cents, shipping_cents, "
        f"delivered_at FROM orders WHERE order_id = {oid}"
    )
    if not found:
        return None
    items = store.query(
        "SELECT i.order_item_id, i.product_id, p.name AS product_name, i.quantity, "
        "i.line_total_cents FROM order_items i LEFT JOIN products p USING (product_id) "
        f"WHERE i.order_id = {oid} ORDER BY i.order_item_id"
    )
    return {**found[0], "items": items}
