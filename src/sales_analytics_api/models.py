"""The response contract. Money is always integer cents; timestamps are UTC."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class DailySales(BaseModel):
    day: date
    channel: str
    orders: int
    revenue_cents: int = Field(description="Shipped and delivered orders only")
    canceled: int
    returned: int


class DailySalesPage(BaseModel):
    items: list[DailySales]
    next_cursor: str | None = Field(description="Pass as ?cursor= to get the next page")


class SummaryRow(BaseModel):
    key: str = Field(description="A channel name or a month (YYYY-MM)")
    orders: int
    revenue_cents: int
    canceled: int
    returned: int


class Summary(BaseModel):
    group_by: str
    start: date
    end: date
    rows: list[SummaryRow]
    total_revenue_cents: int


class CategoryRow(BaseModel):
    category: str
    units: int
    revenue_cents: int


class TopCategories(BaseModel):
    month: str
    rows: list[CategoryRow]


class OrderSummary(BaseModel):
    order_id: int
    ordered_at: datetime
    status: str
    channel: str
    total_cents: int


class OrdersPage(BaseModel):
    items: list[OrderSummary]
    next_cursor: str | None


class OrderItem(BaseModel):
    order_item_id: int
    product_id: int
    product_name: str | None
    quantity: int
    line_total_cents: int


class Order(OrderSummary):
    customer_id: int
    subtotal_cents: int
    discount_cents: int
    shipping_cents: int
    delivered_at: datetime | None
    items: list[OrderItem]


class Health(BaseModel):
    status: str
    data_version: str
    orders: int
    days: int


class Problem(BaseModel):
    """RFC 9457 problem details, served as application/problem+json."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    errors: list[dict] | None = None
