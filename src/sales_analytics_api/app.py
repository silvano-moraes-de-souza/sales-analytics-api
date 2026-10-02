"""The HTTP layer: routes, validation, errors and conditional requests."""

import hashlib
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, queries
from .models import (
    DailySalesPage,
    Health,
    Order,
    OrdersPage,
    Problem,
    Summary,
    TopCategories,
)
from .pagination import BadCursorError
from .store import Store

MAX_RANGE_DAYS = 366
PROBLEM = "application/problem+json"
ERRORS = {
    400: {"model": Problem, "description": "Bad cursor or inconsistent parameters"},
    404: {"model": Problem},
    422: {"model": Problem, "description": "A parameter failed validation"},
}


class GroupBy(StrEnum):
    channel = "channel"
    month = "month"


def _problem(status: int, title: str, detail: str | None = None, errors=None) -> JSONResponse:
    body = Problem(title=title, status=status, detail=detail, errors=errors)
    return JSONResponse(body.model_dump(exclude_none=True), status_code=status, media_type=PROBLEM)


def date_range(
    start: Annotated[date, Query(description="First business day, inclusive")],
    end: Annotated[date, Query(description="Last business day, inclusive")],
) -> tuple[date, date]:
    if end < start:
        raise HTTPException(400, "end is before start")
    if end - start > timedelta(days=MAX_RANGE_DAYS):
        raise HTTPException(400, f"range is longer than {MAX_RANGE_DAYS} days")
    return start, end


Period = Annotated[tuple[date, date], Depends(date_range)]
Limit = Annotated[int, Query(ge=1, le=500, description="Rows per page")]
Cursor = Annotated[str | None, Query(description="next_cursor of the previous page")]


def create_app(lake: Path) -> FastAPI:
    store = Store(lake)
    app = FastAPI(
        title="Sales Analytics API",
        version=__version__,
        description="Read-only analytics over the lakehouse gold and silver layers. "
        "Money is integer cents; timestamps are UTC; business days are UTC-3.",
    )
    app.state.store = store

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        return _problem(exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        errors = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]}
                  for e in exc.errors()]  # fmt: skip
        return _problem(422, "Invalid request parameters", errors=errors)

    @app.exception_handler(BadCursorError)
    async def bad_cursor(_: Request, exc: BadCursorError):
        return _problem(400, "Invalid cursor", str(exc))

    @app.middleware("http")
    async def conditional_get(request: Request, call_next):
        """Data only changes when the gold layer is rebuilt, so an ETag built from
        the data version and the URL lets clients revalidate without a query."""
        if request.method != "GET" or not request.url.path.startswith("/v1/"):
            return await call_next(request)
        tag = hashlib.sha256(f"{store.version}|{request.url.path}?{request.url.query}".encode())
        etag = f'W/"{tag.hexdigest()[:20]}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        response = await call_next(request)
        if response.status_code == 200:
            response.headers["ETag"] = etag
            response.headers["Cache-Control"] = "public, max-age=60"
        return response

    @app.get("/health", response_model=Health, tags=["meta"])
    def health():
        return queries.health(store)

    @app.get("/v1/sales/daily", response_model=DailySalesPage, responses=ERRORS, tags=["sales"])
    def daily_sales(
        period: Period,
        channel: Annotated[str | None, Query(description="Filter by sales channel")] = None,
        limit: Limit = 100,
        cursor: Cursor = None,
    ):
        """Orders and revenue per day and channel, ordered by day then channel."""
        return queries.daily_sales(store, *period, channel, limit, cursor)

    @app.get("/v1/sales/summary", response_model=Summary, responses=ERRORS, tags=["sales"])
    def sales_summary(
        period: Period,
        group_by: GroupBy = GroupBy.channel,
    ):
        """Totals for a period, grouped by channel or by month."""
        return queries.summary(store, *period, group_by.value)

    @app.get("/v1/categories/top", response_model=TopCategories, responses=ERRORS,
             tags=["catalog"])  # fmt: skip
    def top_categories(
        month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM")],
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
    ):
        """Categories ranked by revenue in one month."""
        return queries.top_categories(store, month, limit)

    @app.get("/v1/customers/{customer_id}/orders", response_model=OrdersPage, responses=ERRORS,
             tags=["orders"])  # fmt: skip
    def customer_orders(customer_id: int, limit: Limit = 50, cursor: Cursor = None):
        """Orders of one customer, newest first."""
        page = queries.customer_orders(store, customer_id, limit, cursor)
        if page is None:
            raise HTTPException(404, f"customer {customer_id} not found")
        return page

    @app.get("/v1/orders/{order_id}", response_model=Order, responses=ERRORS, tags=["orders"])
    def order(order_id: int):
        """One order with its line items."""
        found = queries.order(store, order_id)
        if found is None:
            raise HTTPException(404, f"order {order_id} not found")
        return found

    return app
