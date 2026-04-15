from calendar import monthrange
from datetime import datetime, timedelta
import math

from bson import ObjectId
from flask import Blueprint, redirect, render_template, request, session, url_for

from mongo import ensure_connection_or_500
from utils.currency import currency_to_float
from utils.invoices import collect_invoice_items

bp = Blueprint("admin_bp", __name__)

REPORT_LINKS = [
    {"label": "Dashboard", "slug": "dashboard", "href": "admin_bp.reporting"},
    {"label": "Revenue", "slug": "revenue", "href": "admin_bp.reporting_revenue"},
    {"label": "Accounts Receivable", "slug": "accounts-receivable", "href": None},
    {"label": "Jobs", "slug": "jobs", "href": None},
    {"label": "Customers", "slug": "customers", "href": None},
]

def _coerce_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _format_date(value):
    if not value:
        return "-"

    if isinstance(value, datetime):
        return value.strftime("%m/%d/%Y")

    text_value = str(value).strip()
    if not text_value:
        return "-"

    try:
        normalized = text_value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).strftime("%m/%d/%Y")
    except ValueError:
        return text_value


def _parse_datetime(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    text_value = str(value).strip()
    if not text_value:
        return None

    try:
        normalized = text_value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _parse_completed_datetime(value):
    parsed = _parse_datetime(value)
    if parsed:
        return parsed

    text_value = str(value or "").strip()
    if not text_value:
        return None

    try:
        return datetime.strptime(text_value, "%m/%d/%Y %H:%M:%S")
    except ValueError:
        return None


def _nice_axis_max(value):
    if value <= 0:
        return 100.0

    magnitude = 10 ** math.floor(math.log10(value))
    normalized = value / magnitude

    if normalized <= 1:
        nice = 1
    elif normalized <= 2:
        nice = 2
    elif normalized <= 5:
        nice = 5
    else:
        nice = 10

    return nice * magnitude


def _build_daily_revenue_report(db, days=15):
    today = datetime.now().date()
    date_window = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    totals_by_day = {date_key: 0.0 for date_key in date_window}
    completed_jobs_count = 0

    completed_jobs = db.jobs.find(
        {"status": {"$regex": "^Completed$", "$options": "i"}},
        {"total": 1, "dateCompleted": 1},
    )

    for job in completed_jobs:
        completed_at = _parse_completed_datetime(job.get("dateCompleted"))
        if not completed_at:
            continue

        completed_day = completed_at.date()
        if completed_day not in totals_by_day:
            continue

        totals_by_day[completed_day] += currency_to_float(job.get("total"))
        completed_jobs_count += 1

    daily_points = [
        {
            "label": date_key.strftime("%b %d"),
            "iso_date": date_key.isoformat(),
            "total": round(totals_by_day[date_key], 2),
        }
        for date_key in date_window
    ]

    max_revenue = max((point["total"] for point in daily_points), default=0.0)
    axis_max = _nice_axis_max(max_revenue)

    chart_width = 900
    chart_height = 260
    plot_left = 72
    plot_right = 860
    plot_top = 20
    plot_bottom = 196
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top
    point_count = max(1, len(daily_points) - 1)

    chart_points = []
    for index, point in enumerate(daily_points):
        x = plot_left + (plot_width * index / point_count)
        if axis_max <= 0:
            y = plot_bottom
        else:
            y = plot_bottom - (point["total"] / axis_max) * plot_height
        chart_points.append(
            {
                "x": round(x, 2),
                "y": round(y, 2),
                "label": point["label"],
                "total": point["total"],
                "iso_date": point["iso_date"],
            }
        )

    polyline_points = " ".join(f"{point['x']},{point['y']}" for point in chart_points)

    tick_count = 4
    y_ticks = []
    for index in range(tick_count + 1):
        ratio = index / tick_count
        tick_value = round(axis_max * ratio, 2)
        tick_y = round(plot_bottom - (plot_height * ratio), 2)
        y_ticks.append({"y": tick_y, "value": tick_value})

    total_revenue = round(sum(point["total"] for point in daily_points), 2)
    average_daily_revenue = round(total_revenue / max(1, len(daily_points)), 2)
    highest_day = max(daily_points, key=lambda point: point["total"], default={"label": "-", "total": 0.0})

    return {
        "viewport": {
            "width": chart_width,
            "height": chart_height,
        },
        "daily_points": daily_points,
        "chart_points": chart_points,
        "polyline_points": polyline_points,
        "y_ticks": y_ticks,
        "axis_max": axis_max,
        "plot": {
            "left": plot_left,
            "right": plot_right,
            "top": plot_top,
            "bottom": plot_bottom,
        },
        "summary": {
            "window_days": days,
            "total_revenue": total_revenue,
            "average_daily_revenue": average_daily_revenue,
            "completed_jobs": completed_jobs_count,
            "highest_day_label": highest_day.get("label", "-"),
            "highest_day_total": highest_day.get("total", 0.0),
        },
    }


def _build_accounts_receivable_summary(db):
    customer_docs = db.customers.find({}, {"balance_due": 1, "first_name": 1, "last_name": 1, "company": 1})
    completed_jobs = db.jobs.find(
        {"status": {"$regex": "^Completed$", "$options": "i"}},
        {"customer_id": 1, "dateCompleted": 1},
    )

    oldest_completed_by_customer = {}
    for job in completed_jobs:
        customer_id = str(job.get("customer_id") or "").strip()
        if not customer_id:
            continue

        completed_at = _parse_completed_datetime(job.get("dateCompleted"))
        if not completed_at:
            continue

        current_oldest = oldest_completed_by_customer.get(customer_id)
        if current_oldest is None or completed_at < current_oldest:
            oldest_completed_by_customer[customer_id] = completed_at

    total_balance_due = 0.0
    customers_with_balance = 0
    highest_balance_value = 0.0
    highest_balance_customer = "-"
    top_receivables = []
    aging_buckets = {
        "current": {"label": "Current (0-30)", "amount": 0.0, "customers": 0, "severity": "low"},
        "31_60": {"label": "31-60 Days", "amount": 0.0, "customers": 0, "severity": "moderate"},
        "61_90": {"label": "61-90 Days", "amount": 0.0, "customers": 0, "severity": "high"},
        "90_plus": {"label": "90+ Days", "amount": 0.0, "customers": 0, "severity": "critical"},
    }
    today = datetime.now()

    for customer in customer_docs:
        balance_value = currency_to_float(customer.get("balance_due", "$0.00"))
        total_balance_due += balance_value

        if balance_value <= 0:
            continue

        customers_with_balance += 1
        first_name = str(customer.get("first_name") or "").strip()
        last_name = str(customer.get("last_name") or "").strip()
        company = str(customer.get("company") or "").strip()
        name = f"{first_name} {last_name}".strip() or "Customer"
        display_name = f"{name} - {company}" if company else name

        if balance_value > highest_balance_value:
            highest_balance_customer = display_name
            highest_balance_value = balance_value

        oldest_completed = oldest_completed_by_customer.get(str(customer.get("_id") or ""))
        age_days = (today - oldest_completed).days if oldest_completed else 0

        if age_days <= 30:
            bucket_key = "current"
        elif age_days <= 60:
            bucket_key = "31_60"
        elif age_days <= 90:
            bucket_key = "61_90"
        else:
            bucket_key = "90_plus"

        top_receivables.append(
            {
                "customer_id": str(customer.get("_id") or ""),
                "display_name": display_name,
                "balance_due": round(balance_value, 2),
                "severity": aging_buckets[bucket_key]["severity"],
            }
        )

        aging_buckets[bucket_key]["amount"] += balance_value
        aging_buckets[bucket_key]["customers"] += 1

    average_balance_due = total_balance_due / customers_with_balance if customers_with_balance else 0.0
    top_receivables.sort(key=lambda item: item["balance_due"], reverse=True)

    return {
        "total_balance_due": round(total_balance_due, 2),
        "customers_with_balance": customers_with_balance,
        "average_balance_due": round(average_balance_due, 2),
        "highest_balance_customer": highest_balance_customer,
        "highest_balance_value": round(highest_balance_value, 2),
        "top_receivables": top_receivables[:5],
        "aging_buckets": [
            {
                "key": bucket_key,
                "label": bucket["label"],
                "amount": round(bucket["amount"], 2),
                "customers": bucket["customers"],
                "severity": bucket["severity"],
            }
            for bucket_key, bucket in aging_buckets.items()
        ],
    }


def _build_next_billing_date(start_date_value):
    start_date = _parse_datetime(start_date_value)
    if not start_date:
        return "-"

    today = datetime.utcnow()
    target_year = today.year
    target_month = today.month + 1
    if target_month == 13:
        target_month = 1
        target_year += 1

    billing_day = min(start_date.day, monthrange(target_year, target_month)[1])
    next_billing = datetime(target_year, target_month, billing_day)
    return next_billing.strftime("%m/%d/%Y")


def _get_current_employee(db):
    employee_id = session.get("employee_id")
    if not employee_id or not ObjectId.is_valid(employee_id):
        return None

    return db.employees.find_one({"_id": ObjectId(employee_id)})


def _get_subscription_document(db, employee):
    subscription_id = (employee.get("subscription_id") or "").strip()
    if not subscription_id:
        return None

    return db.subscriptions.find_one({"subscription_id": subscription_id})


def _build_subscription_view_model(db, employee, subscription_doc):
    subscription_id = (subscription_doc.get("subscription_id") or employee.get("subscription_id") or "").strip()
    add_ons = []
    for addon in subscription_doc.get("add_ons") or []:
        add_ons.append(
            {
                "id": (addon.get("add_on_id") or "").strip(),
                "name": addon.get("name") or "Add-On",
                "description": addon.get("description") or "",
                "price": _coerce_float(addon.get("price")),
            }
        )

    plan_price = _coerce_float(subscription_doc.get("price"))
    monthly_total = plan_price + sum(addon["price"] for addon in add_ons)
    active_employee_count = db.employees.count_documents({"subscription_id": subscription_id})

    is_cancelled = (subscription_doc.get("status") or "").lower() == "cancelled"
    next_billing_date = "-" if is_cancelled else _build_next_billing_date(subscription_doc.get("start_date"))
    ended_on = _format_date(subscription_doc.get("end_date")) if is_cancelled else None

    return {
        "subscription_id": subscription_id,
        "plan_name": subscription_doc.get("subscription_name") or "Subscription",
        "description": subscription_doc.get("description") or "",
        "plan_price": plan_price,
        "monthly_total": monthly_total,
        "status": (subscription_doc.get("status") or "").replace("_", " ").title() or "-",
        "billing_cycle": (subscription_doc.get("billing_cycle") or "").title() or "-",
        "started_on": _format_date(subscription_doc.get("start_date")),
        "next_billing_date": next_billing_date,
        "renews_on": next_billing_date,
        "active_employees": active_employee_count,
        "current_users": subscription_doc.get("current_users", active_employee_count),
        "max_active_employees": subscription_doc.get("max_users") or 0,
        "add_ons": add_ons,
        "is_cancelled": is_cancelled,
        "ended_on": ended_on,
    }


@bp.route("/admin")
def admin():
    return render_template("admin/admin.html")


@bp.route("/reporting")
def reporting():
    db = ensure_connection_or_500()
    daily_revenue = _build_daily_revenue_report(db, days=15)
    accounts_receivable = _build_accounts_receivable_summary(db)
    return render_template(
        "admin/reporting.html",
        report_links=REPORT_LINKS,
        active_report_slug="dashboard",
        reporting_view_title="Dashboard",
        reporting_view_subtitle="Your most important business signals in one place.",
        daily_revenue_title="Daily Revenue",
        daily_revenue_description="Completed job totals over the last 15 days.",
        dashboard_context_message="This is your reporting home base. Revenue is shown first, and additional cross-report highlights will be added here.",
        accounts_receivable=accounts_receivable,
        daily_revenue=daily_revenue,
    )


@bp.route("/reporting/revenue")
def reporting_revenue():
    db = ensure_connection_or_500()
    daily_revenue = _build_daily_revenue_report(db, days=15)
    return render_template(
        "admin/reporting.html",
        report_links=REPORT_LINKS,
        active_report_slug="revenue",
        reporting_view_title="Revenue",
        reporting_view_subtitle="Revenue performance from completed jobs.",
        daily_revenue_title="Daily Revenue",
        daily_revenue_description="Completed job totals over the last 15 days.",
        dashboard_context_message="",
        accounts_receivable=None,
        daily_revenue=daily_revenue,
    )


@bp.route("/invoices")
def invoices():
    db = ensure_connection_or_500()
    invoice_items = collect_invoice_items(db)
    return render_template("invoices/invoices.html", invoices=invoice_items)


@bp.route("/admin/subscription")
def subscription():
    db = ensure_connection_or_500()
    employee = _get_current_employee(db)
    if not employee:
        return redirect(url_for("auth.login"))

    subscription_doc = _get_subscription_document(db, employee)
    subscription_data = _build_subscription_view_model(db, employee, subscription_doc) if subscription_doc else None

    return render_template(
        "admin/subscription.html",
        subscription=subscription_data,
        subscription_issue=(subscription_data is None),
        cancellation_message=(request.args.get("cancelled") == "1"),
    )


@bp.route("/admin/subscription/manage", methods=["GET", "POST"])
def manage_subscription():
    db = ensure_connection_or_500()
    employee = _get_current_employee(db)
    if not employee:
        return redirect(url_for("auth.login"))

    subscription_doc = _get_subscription_document(db, employee)
    if not subscription_doc:
        return render_template(
            "admin/manage_subscription.html",
            subscription=None,
            subscription_issue=True,
            updated=False,
        )

    if request.method == "POST":
        addon_id = request.form.get("addon_id", "").strip()
        if addon_id:
            db.subscriptions.update_one(
                {"subscription_id": subscription_doc.get("subscription_id")},
                {"$pull": {"add_ons": {"add_on_id": addon_id}}},
            )
        return redirect(url_for("admin_bp.manage_subscription", updated="1"))

    subscription_data = _build_subscription_view_model(db, employee, subscription_doc)
    return render_template(
        "admin/manage_subscription.html",
        subscription=subscription_data,
        subscription_issue=False,
        updated=(request.args.get("updated") == "1"),
    )


@bp.route("/admin/subscription/cancel", methods=["GET", "POST"])
def cancel_subscription():
    db = ensure_connection_or_500()
    employee = _get_current_employee(db)
    if not employee:
        return redirect(url_for("auth.login"))

    subscription_doc = _get_subscription_document(db, employee)
    if not subscription_doc:
        return render_template("admin/cancel_subscription.html", subscription_issue=True)

    if request.method == "POST":
        cancellation_reason = request.form.get("cancellation_reason", "").strip()
        db.subscriptions.update_one(
            {"subscription_id": subscription_doc.get("subscription_id")},
            {
                "$set": {
                    "status": "cancelled",
                    "reason_cancelled": cancellation_reason,
                    "end_date": datetime.utcnow().isoformat() + "Z",
                }
            },
        )
        return redirect(url_for("admin_bp.subscription", cancelled="1"))

    return render_template("admin/cancel_subscription.html", subscription_issue=False)


@bp.route("/admin/subscription/reactivate")
def reactivate_subscription():
    return render_template("admin/reactivate_subscription.html")
