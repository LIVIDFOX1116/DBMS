"""Page-numbered listing for /wallets, /flags, /alerts, /labels, /admin/users.

Additive by construction: response bodies stay plain arrays (the API contract
never changes a body's shape), and everything about the page travels in
headers -- X-Total-Count, X-Page, X-Page-Size and an RFC 8288 Link header.

Pagination only switches on when the caller sends `page` or `page_size`.
Without them an endpoint keeps its existing `limit` behaviour byte for byte.
"""
from typing import Optional

from fastapi import HTTPException, Request, Response
from sqlalchemy import func, select

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500
# Browsers hide response headers from cross-origin scripts unless they are
# exposed by name (the dashboard runs on another origin in dev and in Tauri).
EXPOSED_HEADERS = ["X-Total-Count", "X-Page", "X-Page-Size", "Link"]


def requested(page: Optional[int], page_size: Optional[int]) -> bool:
    return page is not None or page_size is not None


def normalise(page: Optional[int], page_size: Optional[int]) -> tuple[int, int]:
    page = 1 if page is None else page
    page_size = DEFAULT_PAGE_SIZE if page_size is None else page_size
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be 1 or greater")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise HTTPException(status_code=422, detail=f"page_size must be between 1 and {MAX_PAGE_SIZE}")
    return page, page_size


def set_headers(request: Request, response: Response, total: int, page: int, page_size: int) -> None:
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Page"] = str(page)
    response.headers["X-Page-Size"] = str(page_size)
    links = []
    if page * page_size < total:
        links.append(f'<{request.url.include_query_params(page=page + 1, page_size=page_size)}>; rel="next"')
    if page > 1:
        links.append(f'<{request.url.include_query_params(page=page - 1, page_size=page_size)}>; rel="prev"')
    if links:
        response.headers["Link"] = ", ".join(links)


def page_of_list(rows: list, request: Request, response: Response, page: Optional[int], page_size: Optional[int]) -> list:
    """Mock mode: the same pages and headers over an in-memory array."""
    page, page_size = normalise(page, page_size)
    set_headers(request, response, len(rows), page, page_size)
    start = (page - 1) * page_size
    return rows[start:start + page_size]


def page_of_query(session, statement, request: Request, response: Response,
                  page: Optional[int], page_size: Optional[int]) -> list:
    """Live mode: one COUNT plus one LIMIT/OFFSET page of an ORM select. The
    statement must already have a stable ORDER BY (ending in a primary key),
    or rows could repeat or go missing between pages."""
    page, page_size = normalise(page, page_size)
    total = session.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    set_headers(request, response, total, page, page_size)
    rows = session.execute(statement.limit(page_size).offset((page - 1) * page_size))
    return [dict(row._mapping) for row in rows]
