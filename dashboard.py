"""Minimal read-only web dashboard mirroring the Telegram /admin reports.

Run with ``python dashboard.py``. Every request needs HTTP Basic auth from
``DASHBOARD_USERNAME`` / ``DASHBOARD_PASSWORD`` (the process refuses to start
without them). Bind it to localhost and terminate TLS in nginx.

The Telegram handlers already build safe report strings for the reports that
contain no user-controlled text (totals, plans, trend, funnel, panel). Orders
and buyers include buyer-supplied usernames, so those two pages are rendered
here with explicit HTML escaping.
"""

import base64
import hmac
import html
import logging

from aiohttp import web
import aiohttp

import database as db
from config import settings
from handlers.admin import (
    GB,
    _STATUS_FA,
    _TYPE_FA,
    _fmt_dt,
    _funnel_report,
    _panel_report,
    _plans_report,
    _sold_lines,
    _status_key,
    _total_report,
    _trend_report,
)
from panel_api import PanelAPIError, panel

try:
    from tasks import repository
except ImportError:  # analytics module not deployed yet (Kafka WIP)
    repository = None

logger = logging.getLogger(__name__)

WEB_ORDERS_PER_PAGE = 25

_NAV = (
    ("/", "خلاصه"),
    ("/orders", "سفارش‌ها"),
    ("/plans", "پلن‌ها"),
    ("/trend", "روند"),
    ("/funnel", "وضعیت سفارش‌ها"),
    ("/buyers", "خریداران"),
    ("/panel", "وضعیت پنل"),
)

_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;
     background:#0f1217;color:#e6e8eb;line-height:1.6}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;
       padding:14px 20px;background:#161b22;border-bottom:1px solid #2a313c;
       position:sticky;top:0;z-index:5}
header strong{font-size:1.05rem}
nav{display:flex;flex-wrap:wrap;gap:6px}
nav a{color:#b9c2cf;text-decoration:none;padding:5px 11px;border-radius:999px;font-size:.9rem}
nav a:hover{background:#212934;color:#fff}
nav a.active{background:#2563eb;color:#fff}
main{max-width:1180px;margin:0 auto;padding:20px}
h1,h2{font-size:1.1rem;margin:0 0 12px}
section{background:#161b22;border:1px solid #2a313c;border-radius:12px;
        padding:16px 18px;margin-bottom:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;
      margin-bottom:18px}
.card{background:#161b22;border:1px solid #2a313c;border-radius:12px;padding:14px 16px}
.card .label{color:#8b96a5;font-size:.82rem}
.card .value{font-size:1.35rem;font-weight:700;margin-top:4px}
.muted{color:#8b96a5;font-size:.85rem}
.report{white-space:pre-wrap;font-family:inherit;margin:0}
table{width:100%;border-collapse:collapse;font-size:.88rem}
th,td{text-align:right;padding:8px 6px;border-bottom:1px solid #232a34;vertical-align:top}
th{color:#8b96a5;font-weight:600;white-space:nowrap}
tbody tr:hover{background:#1a212b}
.small{color:#8b96a5;font-size:.78rem;direction:ltr;text-align:right}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;background:#212934;
       font-size:.78rem;white-space:nowrap}
.pager{display:flex;justify-content:space-between;align-items:center;margin-top:14px;gap:10px}
.pager a{color:#93b4ff;text-decoration:none}
.table-wrap{overflow-x:auto}
ul.live{margin:8px 0 0;padding-inline-start:20px}
ul.live li{margin:2px 0}
.error{border-color:#7f1d1d;background:#1f1214}
"""

_LIVE_SCRIPT = """
<script>
(function () {
  var status = document.getElementById('live-status');
  function refresh() {
    fetch('/api/live', {cache: 'no-store'}).then(function (r) {
      if (!r.ok) throw new Error('http ' + r.status);
      return r.json();
    }).then(function (d) {
      if (!d.ok) throw new Error(d.error || 'unavailable');
      document.getElementById('live-total').textContent = d.total;
      var list = document.getElementById('live-list');
      var items = (d.by_type || []).map(function (x) {
        var li = document.createElement('li');
        li.textContent = x.event_type + ' — ' + x.count;
        return li;
      });
      list.replaceChildren.apply(list, items);
      status.textContent = ' · آخرین به‌روزرسانی ' + new Date().toLocaleTimeString('fa-IR');
    }).catch(function () {
      status.textContent = ' · رویدادها در دسترس نیست';
    });
  }
  refresh();
  setInterval(refresh, 15000);
})();
</script>
"""


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _money(value) -> str:
    return f"{int(value):,} {settings.CURRENCY}"


def _size(value: int) -> str:
    return "نامحدود" if not value else f"{value / GB:,.1f} گیگابایت"


def _status(value) -> str:
    key = _status_key(value)
    return _STATUS_FA.get(key, str(key))


def _type(value) -> str:
    return _TYPE_FA.get(value, str(value))


def _plan_name(plan_id) -> str:
    plan = settings.get_plan(plan_id or "")
    return plan["name"] if plan else (plan_id or "—")


def _layout(active: str, title: str, body: str) -> str:
    links = "".join(
        '<a class="{}" href="{}">{}</a>'.format(
            "active" if href == active else "", href, _esc(label)
        )
        for href, label in _NAV
    )
    return (
        "<!doctype html><html lang='fa' dir='rtl'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_esc(title)} · {_esc(settings.APP_NAME)}</title>"
        f"<style>{_CSS}</style></head><body>"
        f"<header><strong>{_esc(settings.APP_NAME)}</strong><nav>{links}</nav></header>"
        f"<main><h1>{_esc(title)}</h1>{body}</main></body></html>"
    )


def _card(label: str, value, note: str = "") -> str:
    note_html = f'<div class="muted">{_esc(note)}</div>' if note else ""
    return (
        f'<div class="card"><div class="label">{_esc(label)}</div>'
        f'<div class="value">{_esc(value)}</div>{note_html}</div>'
    )


def _report_card(title: str, report: str) -> str:
    return (
        f"<section><h2>{_esc(title)}</h2>"
        f'<div class="report">{report}</div></section>'
    )


def _check_auth(request: web.Request) -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    user, _, password = decoded.partition(":")
    return hmac.compare_digest(user, settings.DASHBOARD_USERNAME) and hmac.compare_digest(
        password, settings.DASHBOARD_PASSWORD
    )


@web.middleware
async def _basic_auth(request: web.Request, handler):
    if not _check_auth(request):
        return web.Response(
            status=401,
            text="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="vpnbot-admin"'},
        )
    return await handler(request)


@web.middleware
async def _security_headers(request: web.Request, handler):
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
    )
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


async def _home(request: web.Request) -> web.Response:
    rows = await db.get_sales_data()
    sold = list(_sold_lines(rows))
    revenue = sum(price for _, _, price, _ in sold)
    volume = sum(size for _, _, _, size in sold)
    unlimited = sum(1 for _, _, _, size in sold if size == 0)

    cards = (
        '<section class="grid">'
        + _card("سفارش‌های فروخته‌شده", len(sold))
        + _card("درآمد کل", _money(revenue))
        + _card("حجم فروخته‌شده", _size(volume))
        + _card("سفارش نامحدود", unlimited)
        + "</section>"
    )
    live = (
        "<section><h2>فعالیت زنده · ۶۰ ثانیه اخیر</h2>"
        '<p class="muted">کل رویدادها: <b id="live-total">—</b>'
        '<span id="live-status"></span></p>'
        '<ul class="live" id="live-list"></ul>'
        f"<noscript><p class='muted'>برای نمایش زنده، جاوااسکریپت لازم است.</p></noscript>"
        "</section>"
    )
    body = cards + live + _report_card("گزارش فروش کل", _total_report(rows))
    body += _LIVE_SCRIPT
    return web.Response(text=_layout("/", "خلاصه", body), content_type="text/html")


async def _orders(request: web.Request) -> web.Response:
    try:
        page = int(request.query.get("page", "0"))
    except ValueError:
        page = 0
    total = await db.count_orders()
    pages = max((total + WEB_ORDERS_PER_PAGE - 1) // WEB_ORDERS_PER_PAGE, 1)
    page = min(max(page, 0), pages - 1)
    rows = await db.get_orders_page(page * WEB_ORDERS_PER_PAGE, WEB_ORDERS_PER_PAGE)

    if rows:
        body_rows = []
        for r in rows:
            price = r.get("final_price")
            if price is None:
                plan = settings.get_plan(r.get("plan_id") or "")
                price_txt = _money(plan["price"]) if plan else "—"
            else:
                price_txt = _money(price)
            if r.get("telegram_username"):
                buyer = "@" + _esc(r["telegram_username"])
            else:
                buyer = _esc(r.get("telegram_id"))
            detail = " · ".join(
                _esc(part)
                for part in (r.get("requested_username"), r.get("panel_username"))
                if part
            )
            body_rows.append(
                "<tr>"
                f"<td>#{_esc(r.get('order_id'))}</td>"
                f"<td>{_esc(_fmt_dt(r.get('created_at')))}</td>"
                f"<td>{buyer}</td>"
                f"<td>{_esc(_plan_name(r.get('plan_id')))}"
                + (f'<div class="small">{detail}</div>' if detail else "")
                + "</td>"
                f"<td>{_esc(_type(r.get('order_type')))}</td>"
                f"<td><span class='badge'>{_esc(_status(r.get('status')))}</span></td>"
                f"<td>{_esc(price_txt)}</td>"
                f"<td>{_esc(_fmt_dt(r.get('expire_at')))}</td>"
                "</tr>"
            )
        table = (
            '<div class="table-wrap"><table><thead><tr>'
            "<th>#</th><th>تاریخ</th><th>خریدار</th><th>پلن</th><th>نوع</th>"
            "<th>وضعیت</th><th>مبلغ</th><th>انقضا</th></tr></thead><tbody>"
            + "".join(body_rows)
            + "</tbody></table></div>"
        )
    else:
        table = "<p class='muted'>هنوز سفارشی ثبت نشده است.</p>"

    prev_link = (
        f'<a href="/orders?page={page - 1}">« جدیدتر</a>' if page > 0 else "<span></span>"
    )
    next_link = (
        f'<a href="/orders?page={page + 1}">قدیمی‌تر »</a>'
        if page + 1 < pages
        else "<span></span>"
    )
    pager = (
        f'<div class="pager">{prev_link}'
        f'<span class="muted">صفحه {page + 1} از {pages} · {total} سفارش</span>'
        f"{next_link}</div>"
    )
    body = f"<section>{table}{pager}</section>"
    return web.Response(
        text=_layout("/orders", "داشبورد سفارش‌ها", body), content_type="text/html"
    )


async def _plans(request: web.Request) -> web.Response:
    rows = await db.get_sales_data()
    body = _report_card("فروش به تفکیک پلن", _plans_report(rows))
    return web.Response(
        text=_layout("/plans", "فروش به تفکیک پلن", body), content_type="text/html"
    )


async def _trend(request: web.Request) -> web.Response:
    rows = await db.get_sales_data()
    body = _report_card("روند فروش اخیر", _trend_report(rows))
    return web.Response(
        text=_layout("/trend", "روند فروش اخیر", body), content_type="text/html"
    )


async def _funnel(request: web.Request) -> web.Response:
    rows = await db.get_all_orders_for_admin()
    body = _report_card("وضعیت سفارش‌ها", _funnel_report(rows))
    return web.Response(
        text=_layout("/funnel", "وضعیت سفارش‌ها", body), content_type="text/html"
    )


async def _buyers(request: web.Request) -> web.Response:
    rows = await db.get_sales_data()
    agg: dict[int, dict] = {}
    for telegram_id, username, price, size in _sold_lines(rows):
        entry = agg.setdefault(
            telegram_id,
            {"username": None, "orders": 0, "price": 0, "bytes": 0},
        )
        if username and not entry["username"]:
            entry["username"] = username
        entry["orders"] += 1
        entry["price"] += price
        entry["bytes"] += size

    if not agg:
        body = "<section><p class='muted'>هنوز فروش ثبت‌شده‌ای وجود ندارد.</p></section>"
    else:
        table_rows = []
        for telegram_id, entry in sorted(
            agg.items(), key=lambda item: item[1]["price"], reverse=True
        ):
            who = (
                "@" + _esc(entry["username"])
                if entry["username"]
                else _esc(telegram_id)
            )
            table_rows.append(
                "<tr>"
                f"<td>{who}</td>"
                f"<td>{entry['orders']}</td>"
                f"<td>{_esc(_size(entry['bytes']))}</td>"
                f"<td>{_esc(_money(entry['price']))}</td>"
                "</tr>"
            )
        body = (
            "<section><h2>فروش به تفکیک خریدار</h2>"
            '<div class="table-wrap"><table><thead><tr>'
            "<th>خریدار</th><th>سفارش</th><th>حجم</th><th>مبلغ</th>"
            "</tr></thead><tbody>"
            + "".join(table_rows)
            + "</tbody></table></div></section>"
        )
    return web.Response(
        text=_layout("/buyers", "فروش به تفکیک خریدار", body), content_type="text/html"
    )


async def _panel(request: web.Request) -> web.Response:
    try:
        allocation = await panel.get_allocation()
        system = await panel.get_system_stats()
        overview = await panel.get_overview()
        panel_users = await panel.get_users_list()
    except (PanelAPIError, aiohttp.ClientError, OSError) as exc:
        logger.warning("dashboard panel report failed: %s", exc)
        body = (
            "<section class='error'><h2>وضعیت پنل</h2>"
            "<p class='muted'>اتصال به پنل برقرار نشد. دوباره تلاش کنید.</p></section>"
        )
    else:
        tracked = await db.get_all_panel_usernames()
        body = _report_card(
            "پنل در مقابل سفارش‌ها",
            _panel_report(allocation, system, overview, panel_users, tracked),
        )
    return web.Response(
        text=_layout("/panel", "وضعیت پنل", body), content_type="text/html"
    )


async def _api_live(request: web.Request) -> web.Response:
    if repository is None:
        return web.json_response(
            {
                "ok": False,
                "error": "unavailable",
                "seconds": settings.DASHBOARD_LIVE_WINDOW,
                "total": 0,
                "by_type": [],
            }
        )
    try:
        data = await repository.recent_counts(settings.DASHBOARD_LIVE_WINDOW)
    except Exception as exc:
        logger.warning("live events query failed: %s", exc)
        return web.json_response(
            {
                "ok": False,
                "error": "unavailable",
                "seconds": settings.DASHBOARD_LIVE_WINDOW,
                "total": 0,
                "by_type": [],
            }
        )
    return web.json_response({"ok": True, **data})


def create_app() -> web.Application:
    if not settings.DASHBOARD_USERNAME or not settings.DASHBOARD_PASSWORD:
        raise RuntimeError(
            "DASHBOARD_USERNAME and DASHBOARD_PASSWORD must be set in .env"
        )
    app = web.Application(middlewares=[_security_headers, _basic_auth])
    app.add_routes(
        [
            web.get("/", _home),
            web.get("/orders", _orders),
            web.get("/plans", _plans),
            web.get("/trend", _trend),
            web.get("/funnel", _funnel),
            web.get("/buyers", _buyers),
            web.get("/panel", _panel),
            web.get("/api/live", _api_live),
        ]
    )
    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    web.run_app(
        create_app(),
        host=settings.DASHBOARD_HOST,
        port=settings.DASHBOARD_PORT,
        print=None,
    )
