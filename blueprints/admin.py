from calendar import monthrange
import copy
from datetime import UTC, datetime, timedelta
import json
import math
import re

from bson import ObjectId
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from mongo import build_reference_filter, ensure_connection_or_500, reference_value, serialize_doc
from utils.currency import currency_to_float
from utils.notifications import send_email
from utils.security import is_management_position, is_owner_position

bp = Blueprint("admin_bp", __name__)


# Admin endpoints any authenticated employee may use: the admin landing page
# and field reference tools. Everything else in this blueprint (reporting,
# maintenance-plan management, subscription) is restricted to management roles.
_ADMIN_OPEN_ENDPOINTS = {
    "admin",
    "reference",
    "diagnostic_assistant",
    "pt_charts",
}


@bp.before_request
def _require_management_access():
    """Role-based authorization for the admin area.

    Authentication is enforced globally in app.before_request; this adds RBAC so
    non-management employees cannot reach reporting, maintenance-plan, or
    subscription endpoints by direct request. Field tools and the admin landing
    page remain available to all authenticated employees.
    """
    endpoint = (request.endpoint or "").split(".")[-1]
    if endpoint in _ADMIN_OPEN_ENDPOINTS:
        return
    if not is_management_position(session.get("employee_position")):
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "error": "Forbidden"}), 403
        abort(403)

REPORT_LINKS = [
    {"label": "Dashboard", "slug": "dashboard", "href": "admin_bp.reporting"},
    {"label": "Revenue", "slug": "revenue", "href": "admin_bp.reporting_revenue"},
    {"label": "Accounts Receivable", "slug": "accounts-receivable", "href": "admin_bp.reporting_accounts_receivable"},
    {"label": "Jobs", "slug": "jobs", "href": "admin_bp.reporting_jobs"},
    {"label": "Customers", "slug": "customers", "href": "admin_bp.reporting_customers"},
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
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value

    text_value = str(value).strip()
    if not text_value:
        return None

    try:
        normalized = text_value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
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


def _job_completed_datetime(job):
    completed_at = _parse_datetime((job or {}).get("completed_at"))
    if completed_at:
        return completed_at
    return _parse_completed_datetime((job or {}).get("dateCompleted"))


def _job_paid_datetime(job):
    paid_at = _parse_datetime((job or {}).get("paid_at"))
    if paid_at:
        return paid_at

    paid_at = _parse_datetime((job or {}).get("datePaid"))
    if paid_at:
        return paid_at

    return None


def _job_revenue_datetime(job):
    status = str((job or {}).get("status") or "").strip().lower()
    if status == "paid":
        paid_at = _job_paid_datetime(job)
        if paid_at:
            return paid_at

    completed_at = _job_completed_datetime(job)
    if completed_at:
        return completed_at

    return _job_paid_datetime(job)


def _customer_added_datetime(customer):
    created_at = _parse_datetime((customer or {}).get("created_at"))
    if created_at:
        return created_at

    added_value = (customer or {}).get("dateAdded") or (customer or {}).get("date_added")
    parsed_added = _parse_datetime(added_value)
    if parsed_added:
        return parsed_added

    text_value = str(added_value or "").strip()
    if not text_value:
        return None
    try:
        return datetime.strptime(text_value, "%m/%d/%Y")
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


def _build_revenue_performance_report(db, business_id=None):
    today = datetime.now()
    today_date = today.date()

    # Month to date: 1st of this month → today
    mtd_start = today_date.replace(day=1)
    mtd_end = today_date

    # Last month to date: 1st of previous month → same day-of-month
    if today_date.month == 1:
        lmtd_year, lmtd_month = today_date.year - 1, 12
    else:
        lmtd_year, lmtd_month = today_date.year, today_date.month - 1
    lmtd_day = min(today_date.day, monthrange(lmtd_year, lmtd_month)[1])
    lmtd_start = datetime(lmtd_year, lmtd_month, 1).date()
    lmtd_end = datetime(lmtd_year, lmtd_month, lmtd_day).date()

    # Year to date: Jan 1 this year → today
    ytd_start = today_date.replace(month=1, day=1)
    ytd_end = today_date

    # Last year to date: Jan 1 last year → same month/day last year
    lytd_year = today_date.year - 1
    lytd_day = min(today_date.day, monthrange(lytd_year, today_date.month)[1])
    lytd_start = datetime(lytd_year, 1, 1).date()
    lytd_end = datetime(lytd_year, today_date.month, lytd_day).date()

    # 7-day chart window
    chart_days = 7
    date_window = [today_date - timedelta(days=offset) for offset in range(chart_days - 1, -1, -1)]
    totals_by_day = {d: 0.0 for d in date_window}

    mtd_total = 0.0
    lmtd_total = 0.0
    ytd_total = 0.0
    lytd_total = 0.0

    _rev_filter = {"status": {"$in": ["completed", "paid", "Completed", "Paid"]}}
    if business_id:
        _rev_filter["business_id"] = business_id
    completed_jobs = db.jobs.find(
        _rev_filter,
        {
            "total_amount": 1,
            "status": 1,
            "dateCompleted": 1,
            "completed_at": 1,
            "datePaid": 1,
            "paid_at": 1,
        },
    )

    for job in completed_jobs:
        revenue_at = _job_revenue_datetime(job)
        if not revenue_at:
            continue
        completed_day = revenue_at.date()
        amount = _coerce_float(job.get("total_amount"))

        if mtd_start <= completed_day <= mtd_end:
            mtd_total += amount
        if lmtd_start <= completed_day <= lmtd_end:
            lmtd_total += amount
        if ytd_start <= completed_day <= ytd_end:
            ytd_total += amount
        if lytd_start <= completed_day <= lytd_end:
            lytd_total += amount
        if completed_day in totals_by_day:
            totals_by_day[completed_day] += amount

    def _pct_change(current, previous):
        if previous == 0:
            return 100.0 if current > 0 else None
        return round(((current - previous) / previous) * 100, 1)

    mtd_change = _pct_change(mtd_total, lmtd_total)
    ytd_change = _pct_change(ytd_total, lytd_total)

    # 7-day line chart
    daily_points = [
        {
            "label": d.strftime("%b %d"),
            "iso_date": d.isoformat(),
            "total": round(totals_by_day[d], 2),
        }
        for d in date_window
    ]

    max_revenue = max((p["total"] for p in daily_points), default=0.0)
    axis_max = _nice_axis_max(max_revenue)

    chart_width = 900
    chart_height = 240
    plot_left = 72
    plot_right = 860
    plot_top = 20
    plot_bottom = 178
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top
    point_count = max(1, len(daily_points) - 1)

    chart_points = []
    for index, point in enumerate(daily_points):
        x = plot_left + (plot_width * index / point_count)
        y = plot_bottom if axis_max <= 0 else plot_bottom - (point["total"] / axis_max) * plot_height
        chart_points.append({
            "x": round(x, 2),
            "y": round(y, 2),
            "label": point["label"],
            "total": point["total"],
        })

    polyline_points = " ".join(f"{p['x']},{p['y']}" for p in chart_points)

    y_ticks = []
    for i in range(5):
        ratio = i / 4
        y_ticks.append({
            "y": round(plot_bottom - (plot_height * ratio), 2),
            "value": round(axis_max * ratio, 2),
        })

    return {
        "stats": {
            "mtd": round(mtd_total, 2),
            "lmtd": round(lmtd_total, 2),
            "ytd": round(ytd_total, 2),
            "lytd": round(lytd_total, 2),
            "mtd_change": mtd_change,
            "ytd_change": ytd_change,
        },
        "chart": {
            "viewport": {"width": chart_width, "height": chart_height},
            "chart_points": chart_points,
            "polyline_points": polyline_points,
            "y_ticks": y_ticks,
            "plot": {
                "left": plot_left,
                "right": plot_right,
                "top": plot_top,
                "bottom": plot_bottom,
            },
            "window_days": chart_days,
        },
    }


def _build_accounts_receivable_summary(db, business_id=None):
    customer_docs = db.customers.find({}, {"balance_due": 1, "first_name": 1, "last_name": 1, "company": 1})
    _ar_filter = {"status": {"$regex": "^(Completed|Paid)$", "$options": "i"}}
    if business_id:
        _ar_filter["business_id"] = business_id
    completed_jobs = list(db.jobs.find(
        _ar_filter,
        {
            "customer_id": 1,
            "dateCompleted": 1,
            "completed_at": 1,
            "datePaid": 1,
            "paid_at": 1,
            "status": 1,
            "total_amount": 1,
        },
    ))

    oldest_completed_by_customer = {}

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
    overdue_jobs_by_customer = {}
    completed_jobs_for_collection = []

    for job in completed_jobs:
        customer_id = str(job.get("customer_id") or "").strip()
        if not customer_id:
            continue

        completed_at = _job_completed_datetime(job)
        if not completed_at:
            continue

        current_oldest = oldest_completed_by_customer.get(customer_id)
        if current_oldest is None or completed_at < current_oldest:
            oldest_completed_by_customer[customer_id] = completed_at

        if (today - completed_at).days > 30:
            overdue_jobs_by_customer[customer_id] = overdue_jobs_by_customer.get(customer_id, 0) + 1

        completed_jobs_for_collection.append(job)

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
                "bucket_key": bucket_key,
                "severity": aging_buckets[bucket_key]["severity"],
                "repeat_late_payment": overdue_jobs_by_customer.get(str(customer.get("_id") or ""), 0) > 1,
            }
        )

        aging_buckets[bucket_key]["amount"] += balance_value
        aging_buckets[bucket_key]["customers"] += 1

    average_balance_due = total_balance_due / customers_with_balance if customers_with_balance else 0.0
    top_receivables.sort(key=lambda item: item["balance_due"], reverse=True)

    overdue_amount = aging_buckets["31_60"]["amount"] + aging_buckets["61_90"]["amount"] + aging_buckets["90_plus"]["amount"]
    percentage_overdue = (overdue_amount / total_balance_due * 100.0) if total_balance_due > 0 else 0.0

    days_to_collect = []
    overdue_days = []
    paid_jobs_count = 0
    for job in completed_jobs_for_collection:
        completed_at = _job_completed_datetime(job)
        if not completed_at:
            continue
        age_days = (today - completed_at).days
        if age_days > 30:
            overdue_days.append(age_days - 30)

        status = str(job.get("status") or "").strip().lower()
        if status == "paid":
            paid_jobs_count += 1
            paid_at = _job_paid_datetime(job)
            if paid_at:
                delta_days = (paid_at - completed_at).days
                if delta_days >= 0:
                    days_to_collect.append(delta_days)

    collection_denominator = len(completed_jobs_for_collection)
    collection_rate = (paid_jobs_count / collection_denominator * 100.0) if collection_denominator else 0.0
    days_sales_outstanding = (sum(days_to_collect) / len(days_to_collect)) if days_to_collect else 0.0
    avg_days_overdue = (sum(overdue_days) / len(overdue_days)) if overdue_days else 0.0

    return {
        "total_balance_due": round(total_balance_due, 2),
        "customers_with_balance": customers_with_balance,
        "percentage_overdue": round(percentage_overdue, 1),
        "average_balance_due": round(average_balance_due, 2),
        "highest_balance_customer": highest_balance_customer,
        "highest_balance_value": round(highest_balance_value, 2),
        "top_receivables": top_receivables,
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
        "collection_performance": {
            "days_sales_outstanding": round(days_sales_outstanding, 1),
            "collection_rate": round(collection_rate, 1),
            "average_days_overdue": round(avg_days_overdue, 1),
        },
    }


def _build_customer_health_report(db):
    """Return mocked customer health buckets using real customer records."""
    customer_docs = list(
        db.customers.find({}, {"first_name": 1, "last_name": 1, "company": 1})
        .sort([("last_name", 1), ("first_name", 1)])
    )

    buckets = {"overdue": [], "due": [], "good": []}

    for customer in customer_docs:
        customer_id = str(customer.get("_id") or "")
        first_name = str(customer.get("first_name") or "").strip()
        last_name = str(customer.get("last_name") or "").strip()
        company = str(customer.get("company") or "").strip()
        name = f"{first_name} {last_name}".strip() or "Customer"
        display_name = f"{name} - {company}" if company else name

        # Deterministic mock assignment via last hex byte of ObjectId
        last_byte = int(customer_id[-2:], 16) if len(customer_id) >= 2 else 0
        entry = {"customer_id": customer_id, "display_name": display_name}

        if last_byte < 51:
            buckets["overdue"].append(entry)
        elif last_byte < 140:
            buckets["due"].append(entry)
        else:
            buckets["good"].append(entry)

    top_customers = []
    for condition in ("overdue", "due", "good"):
        for item in buckets[condition][:5]:
            top_customers.append(dict(item, condition=condition))

    return {
        "overdue_count": len(buckets["overdue"]),
        "due_count": len(buckets["due"]),
        "good_count": len(buckets["good"]),
        "top_customers": top_customers,
    }


def _parse_scheduled_date(value):
    parsed = _parse_datetime(value)
    if parsed:
        return parsed.date()

    text_value = str(value or "").strip()
    if not text_value:
        return None

    for date_format in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text_value, date_format).date()
        except ValueError:
            continue
    return None


def _build_daily_job_overview_report(db, target_date, business_id=""):
    date_label = target_date.strftime("%m/%d/%Y")

    employee_query = {"business": business_id} if business_id else {}
    employee_docs = list(
        db.employees.find(employee_query, {"first_name": 1, "last_name": 1}).sort([("last_name", 1), ("first_name", 1)])
    )

    rows = []
    rows_by_name = {}
    for employee in employee_docs:
        first_name = str(employee.get("first_name") or "").strip()
        last_name = str(employee.get("last_name") or "").strip()
        employee_name = f"{first_name} {last_name}".strip() or "Employee"
        row = {
            "employee_name": employee_name,
            "estimates_sent": 0,
            "scheduled": 0,
            "completed": 0,
            "total": 0,
        }
        rows.append(row)
        rows_by_name[employee_name] = row

    employee_names = list(rows_by_name.keys())

    estimates_query = {"date_sent": {"$exists": True, "$ne": ""}}
    if employee_names:
        estimates_query["$or"] = [
            {"estimated_by_employee": {"$in": employee_names}},
            {"created_by_employee": {"$in": employee_names}},
        ]

    estimates_cursor = db.estimates.find(
        estimates_query,
        {"estimated_by_employee": 1, "created_by_employee": 1, "date_sent": 1},
    )

    for estimate in estimates_cursor:
        estimate_owner = str(estimate.get("estimated_by_employee") or estimate.get("created_by_employee") or "").strip()
        if estimate_owner not in rows_by_name:
            continue

        sent_date = _parse_scheduled_date(estimate.get("date_sent"))
        if sent_date == target_date:
            rows_by_name[estimate_owner]["estimates_sent"] += 1

    jobs_query = {
        "$or": [
            {"dateScheduled": {"$exists": True, "$ne": ""}},
            {"dateCompleted": {"$exists": True, "$ne": ""}},
        ]
    }
    if employee_names:
        jobs_query["assigned_employee"] = {"$in": employee_names}

    jobs_cursor = db.jobs.find(
        jobs_query,
        {"assigned_employee": 1, "dateScheduled": 1, "dateCompleted": 1, "scheduled_at": 1, "completed_at": 1},
    )

    for job in jobs_cursor:
        assigned_employee = str(job.get("assigned_employee") or "").strip()
        if assigned_employee not in rows_by_name:
            continue

        row = rows_by_name[assigned_employee]
        scheduled_at = _parse_datetime(job.get("scheduled_at"))
        date_scheduled = scheduled_at.date() if scheduled_at else _parse_scheduled_date(job.get("dateScheduled"))
        if date_scheduled == target_date:
            row["scheduled"] += 1

        completed_at = _job_completed_datetime(job)
        if completed_at and completed_at.date() == target_date:
            row["completed"] += 1

    for row in rows:
        row["total"] = row["estimates_sent"] + row["scheduled"] + row["completed"]

    summary = {
        "estimates_sent": sum(row["estimates_sent"] for row in rows),
        "scheduled": sum(row["scheduled"] for row in rows),
        "completed": sum(row["completed"] for row in rows),
        "total": sum(row["total"] for row in rows),
        "employee_count": len(rows),
    }

    return {
        "date_label": date_label,
        "rows": rows,
        "summary": summary,
    }


def _build_next_billing_date(start_date_value):
    start_date = _parse_datetime(start_date_value)
    if not start_date:
        return "-"

    today = datetime.now(UTC)
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
    plan_price = _coerce_float(subscription_doc.get("price"))
    monthly_total = plan_price

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
        "current_users": subscription_doc.get("current_users") or 0,
        "max_active_employees": subscription_doc.get("max_users") or 0,
        "is_cancelled": is_cancelled,
        "ended_on": ended_on,
    }


def _build_revenue_report_data(db, start_dt, end_dt, business_id=None):
    """Build revenue metrics for a given datetime range."""
    yoy_start_dt = start_dt.replace(year=start_dt.year - 1)
    yoy_end_dt = end_dt.replace(year=end_dt.year - 1)

    today = datetime.now(UTC).date()
    cur_month_start = datetime(today.year, today.month, 1)
    if today.month == 1:
        prev_month_year, prev_month = today.year - 1, 12
    else:
        prev_month_year, prev_month = today.year, today.month - 1
    prev_month_start = datetime(prev_month_year, prev_month, 1)
    prev_month_end_day = monthrange(prev_month_year, prev_month)[1]
    prev_month_end = datetime(prev_month_year, prev_month, prev_month_end_day, 23, 59, 59)
    if prev_month_year == today.year and prev_month == 1:
        two_months_ago_year, two_months_ago = prev_month_year - 1, 12
    else:
        two_months_ago_year = prev_month_year
        two_months_ago = prev_month - 1
        if two_months_ago == 0:
            two_months_ago_year -= 1
            two_months_ago = 12
    two_months_ago_start = datetime(two_months_ago_year, two_months_ago, 1)
    two_months_ago_end_day = monthrange(two_months_ago_year, two_months_ago)[1]
    two_months_ago_end = datetime(two_months_ago_year, two_months_ago, two_months_ago_end_day, 23, 59, 59)

    rev_filter = {"status": {"$in": ["completed", "paid", "Completed", "Paid"]}}
    if business_id:
        rev_filter["business_id"] = business_id

    all_jobs = list(db.jobs.find(
        rev_filter,
        {
            "total_amount": 1,
            "customer_id": 1,
            "invoices": 1,
            "completed_at": 1,
            "dateCompleted": 1,
            "paid_at": 1,
            "datePaid": 1,
            "status": 1,
            "services": 1,
            "equipments": 1,
            "assigned_employee": 1,
        },
    ))

    def _sum_jobs_in_range(jobs, range_start, range_end):
        total = 0.0
        count = 0
        contract_count = 0
        for job in jobs:
            revenue_at = _job_revenue_datetime(job)
            if not revenue_at:
                continue
            if range_start <= revenue_at <= range_end:
                amount = _coerce_float(job.get("total_amount"))
                total += amount
                count += 1
                for svc in (job.get("services") or []):
                    if (svc.get("service_type") or "").strip() == "Service Agreement / Contracts":
                        contract_count += 1
                        break
        return total, count, contract_count

    # Selected range
    range_total, range_count, range_contracts = _sum_jobs_in_range(all_jobs, start_dt, end_dt)
    # YoY range
    yoy_total, yoy_count, yoy_contracts = _sum_jobs_in_range(all_jobs, yoy_start_dt, yoy_end_dt)

    def _pct(current, previous):
        if previous == 0:
            return None
        return round(((current - previous) / previous) * 100.0, 1)

    total_revenue_yoy_pct = _pct(range_total, yoy_total)
    avg_job_value = round(range_total / range_count, 2) if range_count else 0.0
    avg_job_value_yoy = round(yoy_total / yoy_count, 2) if yoy_count else 0.0
    avg_job_value_yoy_pct = _pct(avg_job_value, avg_job_value_yoy)
    contracts_yoy_pct = _pct(range_contracts, yoy_contracts)

    # MOM growth (always current month vs previous month, independent of filter range)
    cur_month_total, _, _ = _sum_jobs_in_range(all_jobs, cur_month_start, datetime.now(UTC).replace(tzinfo=None))
    prev_month_total, _, _ = _sum_jobs_in_range(all_jobs, prev_month_start, prev_month_end)
    two_months_ago_total, _, _ = _sum_jobs_in_range(all_jobs, two_months_ago_start, two_months_ago_end)
    mom_pct = _pct(cur_month_total, prev_month_total)
    prev_mom_pct = _pct(prev_month_total, two_months_ago_total)

    # Daily bars for the selected range
    delta_days = (end_dt.date() - start_dt.date()).days + 1
    bars = []
    for offset in range(delta_days):
        day_date = start_dt.date() + timedelta(days=offset)
        day_start = datetime(day_date.year, day_date.month, day_date.day, 0, 0, 0)
        day_end = datetime(day_date.year, day_date.month, day_date.day, 23, 59, 59)
        day_total, _, _ = _sum_jobs_in_range(all_jobs, day_start, day_end)
        bars.append({"label": day_date.strftime("%b %d"), "amount": round(day_total, 2)})

    # Revenue by service type
    service_type_totals = {}
    for job in all_jobs:
        revenue_at = _job_revenue_datetime(job)
        if not revenue_at or not (start_dt <= revenue_at <= end_dt):
            continue
        job_amount = _coerce_float(job.get("total_amount"))
        services_list = job.get("services") or []
        if not services_list:
            service_type_totals.setdefault("Uncategorized", 0.0)
            service_type_totals["Uncategorized"] += job_amount
        else:
            per_service = job_amount / len(services_list) if services_list else 0.0
            for svc in services_list:
                stype = (svc.get("service_type") or "Uncategorized").strip() or "Uncategorized"
                service_type_totals[stype] = service_type_totals.get(stype, 0.0) + per_service

    sorted_types = sorted(service_type_totals.items(), key=lambda x: x[1], reverse=True)
    max_type_amount = sorted_types[0][1] if sorted_types else 1.0
    revenue_by_service_type = [
        {
            "service_type": stype,
            "amount": round(amount, 2),
            "pct_of_max": round((amount / max_type_amount) * 100) if max_type_amount > 0 else 0,
        }
        for stype, amount in sorted_types
    ]

    # Revenue by equipment type
    equipment_type_totals = {}
    employee_totals = {}
    for job in all_jobs:
        revenue_at = _job_revenue_datetime(job)
        if not revenue_at or not (start_dt <= revenue_at <= end_dt):
            continue

        job_amount = _coerce_float(job.get("total_amount"))

        equipment_list = job.get("equipments") or []
        if equipment_list:
            per_equipment = job_amount / len(equipment_list)
            for equipment in equipment_list:
                equipment_type = (
                    (equipment.get("equipment_type") or "").strip()
                    or (equipment.get("category") or "").strip()
                    or (equipment.get("equipment_name") or "").strip()
                    or "Uncategorized"
                )
                equipment_type_totals[equipment_type] = equipment_type_totals.get(equipment_type, 0.0) + per_equipment

        employee_name = (job.get("assigned_employee") or "").strip() or "Unassigned"
        employee_totals[employee_name] = employee_totals.get(employee_name, 0.0) + job_amount

    sorted_equipment = sorted(equipment_type_totals.items(), key=lambda x: x[1], reverse=True)
    max_equipment_amount = sorted_equipment[0][1] if sorted_equipment else 1.0
    revenue_by_equipment_type = [
        {
            "equipment_type": equipment_type,
            "amount": round(amount, 2),
            "pct_of_max": round((amount / max_equipment_amount) * 100) if max_equipment_amount > 0 else 0,
        }
        for equipment_type, amount in sorted_equipment
    ]

    sorted_employees = sorted(employee_totals.items(), key=lambda x: x[1], reverse=True)
    if len(sorted_employees) > 5:
        top_employees = sorted_employees[:5]
        other_amount = sum(amount for _, amount in sorted_employees[5:])
        if other_amount > 0:
            top_employees.append(("Other", other_amount))
        sorted_employees = top_employees

    employee_total_revenue = sum(amount for _, amount in sorted_employees)
    revenue_by_employee = [
        {
            "employee": employee,
            "amount": round(amount, 2),
            "pct": round((amount / employee_total_revenue) * 100, 1) if employee_total_revenue > 0 else 0.0,
        }
        for employee, amount in sorted_employees
    ]

    customer_ids = {
        str(job.get("customer_id") or "").strip()
        for job in all_jobs
        if str(job.get("customer_id") or "").strip()
    }
    customer_map = {}
    if customer_ids:
        for customer in db.customers.find({"_id": {"$in": [ObjectId(cid) for cid in customer_ids if ObjectId.is_valid(cid)]}}, {"created_at": 1, "date_added": 1, "dateAdded": 1}):
            customer_map[str(customer.get("_id"))] = _customer_added_datetime(customer)

    returning_total = 0.0
    new_total = 0.0
    for job in all_jobs:
        revenue_at = _job_revenue_datetime(job)
        if not revenue_at or not (start_dt <= revenue_at <= end_dt):
            continue

        amount = _coerce_float(job.get("total_amount"))
        customer_id = str(job.get("customer_id") or "").strip()
        added_at = customer_map.get(customer_id)
        age_days = (revenue_at - added_at).days if added_at else None
        if age_days is not None and age_days > 30:
            returning_total += amount
        else:
            new_total += amount

    max_new_vs_returning = max(returning_total, new_total, 1.0)
    revenue_new_vs_returning = [
        {
            "label": "Returning",
            "amount": round(returning_total, 2),
            "pct_of_max": round((returning_total / max_new_vs_returning) * 100),
        },
        {
            "label": "New",
            "amount": round(new_total, 2),
            "pct_of_max": round((new_total / max_new_vs_returning) * 100),
        },
    ]

    return {
        "total_revenue": round(range_total, 2),
        "total_revenue_yoy_pct": total_revenue_yoy_pct,
        "avg_job_value": avg_job_value,
        "avg_job_value_yoy_pct": avg_job_value_yoy_pct,
        "active_contracts": range_contracts,
        "active_contracts_yoy_pct": contracts_yoy_pct,
        "mom_pct": mom_pct,
        "prev_mom_pct": prev_mom_pct,
        "bars": bars,
        "bars_max": max(b["amount"] for b in bars) if bars else 0.0,
        "revenue_by_service_type": revenue_by_service_type,
        "revenue_by_equipment_type": revenue_by_equipment_type,
        "revenue_by_employee": revenue_by_employee,
        "revenue_new_vs_returning": revenue_new_vs_returning,
    }


# ---------------------------------------------------------------------------
# Maintenance Plan Templates — builder
# ---------------------------------------------------------------------------

def _build_maintenance_plan_template_document(data, business_id, now=None):
    """Construct a maintenance_plan_templates document from form/API input.

    ``data`` may be a dict (JSON payload) or a Flask ImmutableMultiDict
    (form submission).  ``business_id`` must already be resolved by the
    caller as an ObjectId or string.
    """
    if now is None:
        now = datetime.now(UTC)

    def _get(key, default=None):
        if hasattr(data, "get"):
            return data.get(key, default)
        return default

    def _bool_field(key, default=False):
        raw = _get(key)
        if isinstance(raw, bool):
            return raw
        if raw in (None, ""):
            return default
        return str(raw or "").strip().lower() in {"true", "1", "yes", "on"}

    def _int_field(key, default=None):
        raw = _get(key)
        if raw in (None, ""):
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def _float_field(key, default=None):
        raw = _get(key)
        if raw in (None, ""):
            return default
        try:
            return round(float(raw), 4)
        except (TypeError, ValueError):
            return default

    def _list_field(key):
        import json as _json
        raw = _get(key)
        if isinstance(raw, list):
            return raw
        if raw in (None, ""):
            return []
        try:
            parsed = _json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []

    visit_seasons_raw = _list_field("visit_seasons")
    normalized_visit_seasons = []
    for entry in visit_seasons_raw:
        if isinstance(entry, dict):
            season_value = str(entry.get("season") or "").strip()
            if not season_value:
                continue

            service_id_text = str(entry.get("service_id") or "").strip()
            if ObjectId.is_valid(service_id_text):
                service_id_value = ObjectId(service_id_text)
            else:
                service_id_value = service_id_text or None

            service_name_value = str(entry.get("service_name") or "").strip() or None
            start_date_value = str(entry.get("start_date") or "").strip() or None
            end_date_value = str(entry.get("end_date") or "").strip() or None
            if season_value != "Custom":
                start_date_value = None
                end_date_value = None

            normalized_visit_seasons.append(
                {
                    "season": season_value,
                    "service_id": service_id_value,
                    "service_name": service_name_value,
                    "start_date": start_date_value,
                    "end_date": end_date_value,
                }
            )
        else:
            season_value = str(entry or "").strip()
            if season_value:
                normalized_visit_seasons.append(
                    {
                        "season": season_value,
                        "service_id": None,
                        "service_name": None,
                        "start_date": None,
                        "end_date": None,
                    }
                )

    return {
        "business_id": business_id,
        "name": str(_get("name") or "").strip(),
        "description": str(_get("description") or "").strip() or None,
        "tier_order": _int_field("tier_order"),
        "is_active": _bool_field("is_active", default=True),
        "visits_per_year": _int_field("visits_per_year", default=1),
        "visit_seasons": normalized_visit_seasons,
        "price_annual": _float_field("price_annual", default=0.0),
        "price_monthly": _float_field("price_monthly"),
        "repair_discount_pct": _float_field("repair_discount_pct"),
        "discount_service_types": _list_field("discount_service_types"),
        "discount_line_item_types": _list_field("discount_line_item_types"),
        "diagnostic_fee_waived": _bool_field("diagnostic_fee_waived"),
        "priority_scheduling": _bool_field("priority_scheduling"),
        "emergency_service": _bool_field("emergency_service"),
        "custom_benefits": _list_field("custom_benefits"),
        "created_at": now,
        "updated_at": now,
    }


def _build_maintenance_plan_document(data, business_id, now=None):
    """Construct a maintenance_plans document from form/API input."""
    if now is None:
        now = datetime.now(UTC)

    def _get(key, default=None):
        if hasattr(data, "get"):
            return data.get(key, default)
        return default

    def _bool_field(key, default=False):
        raw = _get(key)
        if isinstance(raw, bool):
            return raw
        if raw in (None, ""):
            return default
        return str(raw or "").strip().lower() in {"true", "1", "yes", "on"}

    def _int_field(key, default=None):
        raw = _get(key)
        if raw in (None, ""):
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def _float_field(key, default=None):
        raw = _get(key)
        if raw in (None, ""):
            return default
        try:
            return round(float(raw), 4)
        except (TypeError, ValueError):
            return default

    def _string_field(key, default=""):
        raw = _get(key)
        if raw in (None, ""):
            return default
        return str(raw).strip()

    def _json_or_list_field(key):
        raw = _get(key)
        if isinstance(raw, list):
            return raw
        if raw in (None, ""):
            return []
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []

    def _json_or_dict_field(key):
        raw = _get(key)
        if isinstance(raw, dict):
            return copy.deepcopy(raw)
        if raw in (None, ""):
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _date_field(key, default=None):
        raw = _get(key)
        parsed = _parse_datetime(raw)
        if parsed:
            return parsed
        return default

    plan_id = ObjectId()
    start_date = _date_field("start_date", default=now)
    end_date = start_date + timedelta(days=365) if start_date else None

    property_address_input = _json_or_dict_field("property_address")
    property_address = {
        "address_line_1": _string_field("property_address_line_1", property_address_input.get("address_line_1", "")),
        "address_line_2": _string_field("property_address_line_2", property_address_input.get("address_line_2", "")) or None,
        "city": _string_field("property_address_city", property_address_input.get("city", "")),
        "state": _string_field("property_address_state", property_address_input.get("state", "")),
        "zip_code": _string_field("property_address_zip_code", property_address_input.get("zip_code", "")),
    }

    covered_systems = []
    for entry in _json_or_list_field("covered_systems"):
        if not isinstance(entry, dict):
            continue
        raw_system_id = str(entry.get("hvac_system_id") or "").strip()
        covered_systems.append(
            {
                "hvac_system_id": ObjectId(raw_system_id) if ObjectId.is_valid(raw_system_id) else raw_system_id or None,
                "system_nickname": str(entry.get("system_nickname") or "").strip(),
                "system_type": str(entry.get("system_type") or "").strip(),
                "system_tonnage": str(entry.get("system_tonnage") or "").strip(),
                "manufacturer": str(entry.get("manufacturer") or "").strip(),
                "manufactured_year": str(entry.get("manufactured_year") or "").strip(),
            }
        )

    billing_history = []
    for entry in _json_or_list_field("billing_history"):
        if not isinstance(entry, dict):
            continue
        raw_invoice_id = str(entry.get("invoice_id") or "").strip()
        amount_value = entry.get("amount")
        try:
            normalized_amount = round(float(amount_value), 4) if amount_value not in (None, "") else 0.0
        except (TypeError, ValueError):
            normalized_amount = 0.0
        billing_history.append(
            {
                "date": _parse_datetime(entry.get("date")),
                "amount": normalized_amount,
                "status": str(entry.get("status") or "").strip(),
                "invoice_id": ObjectId(raw_invoice_id) if ObjectId.is_valid(raw_invoice_id) else raw_invoice_id or None,
            }
        )

    series_ids = []
    for raw_series_id in _json_or_list_field("series_ids"):
        series_id_text = str(raw_series_id or "").strip()
        if ObjectId.is_valid(series_id_text):
            series_ids.append(ObjectId(series_id_text))
        elif series_id_text:
            series_ids.append(series_id_text)

    billing_type = _string_field("billing_type", default="annual").lower() or "annual"
    next_billing_date = _date_field("next_billing_date", default=start_date)
    template_snapshot = _json_or_dict_field("template_snapshot")

    raw_template_id = _string_field("template_id", default="")
    raw_customer_id = _string_field("customer_id", default="")
    raw_property_id = _string_field("property_id", default="")

    return {
        "_id": plan_id,
        "plan_number": f"MP-{str(plan_id)[-6:].upper()}",
        "business_id": business_id,
        "template_id": ObjectId(raw_template_id) if ObjectId.is_valid(raw_template_id) else raw_template_id or None,
        "template_snapshot": template_snapshot,
        "customer_id": ObjectId(raw_customer_id) if ObjectId.is_valid(raw_customer_id) else raw_customer_id or None,
        "customer_name": _string_field("customer_name"),
        "company": _string_field("company"),
        "property_id": ObjectId(raw_property_id) if ObjectId.is_valid(raw_property_id) else raw_property_id or None,
        "property_name": _string_field("property_name"),
        "property_address": property_address,
        "covered_systems": covered_systems,
        "status": _string_field("status", default="active").lower() or "active",
        "start_date": start_date,
        "end_date": end_date,
        "renewal_date": end_date - timedelta(days=30) if end_date else None,
        "auto_renew": _bool_field("auto_renew", default=False),
        "billing_type": billing_type,
        "billing_amount": _float_field("billing_amount", default=0.0),
        "next_billing_date": next_billing_date,
        "billing_history": billing_history,
        "series_ids": series_ids,
        "visits_scheduled": _int_field("visits_scheduled", default=0),
        "visits_completed": _int_field("visits_completed", default=0),
        "last_visit_date": _date_field("last_visit_date"),
        "next_visit_date": _date_field("next_visit_date"),
        "sold_by_employee_id": _string_field("sold_by_employee_id"),
        "sold_by_name": _string_field("sold_by_name"),
        "sold_via": _string_field("sold_via", default="office").lower() or "office",
        "created_at": now,
        "updated_at": now,
        "cancelled_at": _date_field("cancelled_at"),
        "cancellation_reason": _string_field("cancellation_reason") or None,
    }


def _find_maintenance_plan_property(customer, property_id):
    """Return the embedded property dict matching ``property_id`` for a customer."""
    normalized = str(property_id or "").strip()
    if not normalized:
        return None
    for prop in (customer or {}).get("properties", []) or []:
        if not isinstance(prop, dict):
            continue
        if str(prop.get("property_id") or "").strip() == normalized:
            return prop
    return None


def _maintenance_visit_anchor_datetime(season, start_date):
    """Resolve the seasonal anchor date for a maintenance visit series.

    Returns a naive ``datetime``. If the seasonal date has already passed
    relative to ``start_date`` the same date in the following year is used.
    """
    base = start_date
    if isinstance(base, datetime) and base.tzinfo is not None:
        base = base.replace(tzinfo=None)
    if not isinstance(base, datetime):
        base = datetime.now(UTC).replace(tzinfo=None)

    season_months = {
        "spring": (4, 15),
        "summer": (7, 15),
        "fall": (10, 15),
        "winter": (1, 15),
    }
    month_day = season_months.get(str(season or "").strip().lower())
    if not month_day:
        return base

    month, day = month_day
    candidate = datetime(base.year, month, day)
    if candidate < base.replace(hour=0, minute=0, second=0, microsecond=0):
        candidate = datetime(base.year + 1, month, day)
    return candidate


def _maintenance_template_business_scope_predicate(business_id):
    if isinstance(business_id, ObjectId):
        return {"$or": [{"business_id": business_id}, {"business_id": str(business_id)}]}

    business_text = str(business_id or "").strip()
    if ObjectId.is_valid(business_text):
        business_oid = ObjectId(business_text)
        return {"$or": [{"business_id": business_oid}, {"business_id": str(business_oid)}]}

    return {"business_id": business_text}


def _resolve_current_business_ref(current_employee):
    business_ref = (current_employee or {}).get("business")
    if isinstance(business_ref, ObjectId):
        return business_ref
    if ObjectId.is_valid(str(business_ref or "")):
        return ObjectId(str(business_ref))
    return str(business_ref or "").strip()


def _fetch_active_service_options(db, business_id):
    business_query = _maintenance_template_business_scope_predicate(business_id)
    query = {
        "$and": [
            business_query,
            {"$or": [{"is_active": {"$exists": False}}, {"is_active": True}]},
        ]
    }

    services = []
    for service in db.services.find(query, {"_id": 1, "service_name": 1}).sort("service_name", 1):
        services.append(
            {
                "_id": str(service.get("_id") or "").strip(),
                "service_name": str(service.get("service_name") or "").strip(),
            }
        )
    return services


def _build_template_form_payload_from_request(request_obj):
    season_values = request_obj.form.getlist("visit_slot_season[]")
    service_ids = request_obj.form.getlist("visit_slot_service_id[]")
    service_names = request_obj.form.getlist("visit_slot_service_name[]")
    window_starts = request_obj.form.getlist("visit_window_start[]")
    window_ends = request_obj.form.getlist("visit_window_end[]")
    visit_seasons = []
    for index, season_raw in enumerate(season_values):
        season = str(season_raw or "").strip()
        if not season:
            continue

        service_id = str(service_ids[index] if index < len(service_ids) else "").strip()
        service_name = str(service_names[index] if index < len(service_names) else "").strip()
        start_value = None
        end_value = None
        if season == "Custom":
            start_value = str(window_starts[index] if index < len(window_starts) else "").strip() or None
            end_value = str(window_ends[index] if index < len(window_ends) else "").strip() or None

        visit_seasons.append(
            {
                "season": season,
                "service_id": service_id or None,
                "service_name": service_name or None,
                "start_date": start_value,
                "end_date": end_value,
            }
        )

    custom_benefits = [
        str(value or "").strip()
        for value in request_obj.form.getlist("custom_benefits[]")
        if str(value or "").strip()
    ]

    discount_service_types = [
        str(value or "").strip()
        for value in request_obj.form.getlist("discount_service_types[]")
        if str(value or "").strip()
    ]
    discount_line_item_types = [
        str(value or "").strip()
        for value in request_obj.form.getlist("discount_line_item_types[]")
        if str(value or "").strip()
    ]

    return {
        "name": request_obj.form.get("name", ""),
        "description": request_obj.form.get("description", ""),
        "tier_order": request_obj.form.get("tier_order", ""),
        "is_active": True,
        "visits_per_year": request_obj.form.get("visits_per_year", ""),
        "visit_seasons": visit_seasons,
        "price_annual": request_obj.form.get("price_annual", ""),
        "price_monthly": request_obj.form.get("price_monthly", ""),
        "repair_discount_pct": request_obj.form.get("repair_discount_pct", ""),
        "discount_service_types": discount_service_types,
        "discount_line_item_types": discount_line_item_types,
        "diagnostic_fee_waived": request_obj.form.get("diagnostic_fee_waived", ""),
        "priority_scheduling": request_obj.form.get("priority_scheduling", ""),
        "emergency_service": request_obj.form.get("emergency_service", ""),
        "custom_benefits": custom_benefits,
    }


def _normalize_template_for_form(template_doc):
    if not template_doc:
        return None

    normalized = dict(template_doc)
    normalized["_id"] = str(template_doc.get("_id") or "").strip()
    legacy_windows_by_slot = {}
    for window_entry in list(template_doc.get("visit_season_windows") or []):
        slot = int(window_entry.get("slot") or 0)
        if slot > 0:
            legacy_windows_by_slot[slot] = {
                "start_date": str(window_entry.get("start_date") or "").strip() or None,
                "end_date": str(window_entry.get("end_date") or "").strip() or None,
            }

    legacy_service_id = str(template_doc.get("visit_service_id") or "").strip() or None
    normalized_visit_seasons = []
    for index, entry in enumerate(list(template_doc.get("visit_seasons") or []), start=1):
        if isinstance(entry, dict):
            season_value = str(entry.get("season") or "").strip()
            if not season_value:
                continue
            normalized_visit_seasons.append(
                {
                    "season": season_value,
                    "service_id": str(entry.get("service_id") or "").strip() or None,
                    "service_name": str(entry.get("service_name") or "").strip() or None,
                    "start_date": str(entry.get("start_date") or "").strip() or None,
                    "end_date": str(entry.get("end_date") or "").strip() or None,
                }
            )
        else:
            season_value = str(entry or "").strip()
            if not season_value:
                continue
            window_data = legacy_windows_by_slot.get(index, {})
            normalized_visit_seasons.append(
                {
                    "season": season_value,
                    "service_id": legacy_service_id,
                    "service_name": None,
                    "start_date": window_data.get("start_date") if season_value == "Custom" else None,
                    "end_date": window_data.get("end_date") if season_value == "Custom" else None,
                }
            )

    normalized["visit_seasons"] = normalized_visit_seasons
    normalized["discount_service_types"] = [
        str(value or "").strip() for value in (template_doc.get("discount_service_types") or [])
    ]
    normalized["discount_line_item_types"] = [
        str(value or "").strip() for value in (template_doc.get("discount_line_item_types") or [])
    ]
    normalized["custom_benefits"] = [str(value or "").strip() for value in (template_doc.get("custom_benefits") or [])]
    return normalized


def _normalize_form_payload_for_template(form_payload, default_active=True):
    return {
        "name": str(form_payload.get("name") or "").strip(),
        "description": str(form_payload.get("description") or "").strip(),
        "tier_order": str(form_payload.get("tier_order") or "").strip(),
        "is_active": str(form_payload.get("is_active") or "").strip().lower() in {"true", "1", "yes", "on"}
        if "is_active" in form_payload
        else default_active,
        "visits_per_year": str(form_payload.get("visits_per_year") or "").strip(),
        "visit_seasons": list(form_payload.get("visit_seasons") or []),
        "price_annual": str(form_payload.get("price_annual") or "").strip(),
        "price_monthly": str(form_payload.get("price_monthly") or "").strip(),
        "repair_discount_pct": str(form_payload.get("repair_discount_pct") or "").strip(),
        "discount_service_types": [
            str(value or "").strip() for value in (form_payload.get("discount_service_types") or [])
        ],
        "discount_line_item_types": [
            str(value or "").strip() for value in (form_payload.get("discount_line_item_types") or [])
        ],
        "diagnostic_fee_waived": str(form_payload.get("diagnostic_fee_waived") or "").strip().lower() in {"true", "1", "yes", "on"},
        "priority_scheduling": str(form_payload.get("priority_scheduling") or "").strip().lower() in {"true", "1", "yes", "on"},
        "emergency_service": str(form_payload.get("emergency_service") or "").strip().lower() in {"true", "1", "yes", "on"},
        "custom_benefits": [str(value or "").strip() for value in (form_payload.get("custom_benefits") or [])],
    }


def _validate_maintenance_template_payload(payload, db, business_id, exclude_template_id=None):
    errors = {}

    required_fields = [
        "name",
        "description",
        "tier_order",
        "visits_per_year",
        "price_annual",
    ]
    for field in required_fields:
        value = payload.get(field)
        if value is None or str(value).strip() == "":
            errors[field] = "This field is required."

    tier_order = None
    if "tier_order" not in errors:
        try:
            tier_order = int(payload.get("tier_order"))
            if tier_order < 1:
                errors["tier_order"] = "Tier order must be 1 or greater."
        except (TypeError, ValueError):
            errors["tier_order"] = "Tier order must be a whole number."

    try:
        visits_per_year = int(payload.get("visits_per_year"))
        if visits_per_year < 1:
            errors["visits_per_year"] = "Visits per year must be 1 or greater."
    except (TypeError, ValueError):
        if "visits_per_year" not in errors:
            errors["visits_per_year"] = "Visits per year must be a whole number."

    visit_entries = list(payload.get("visit_seasons") or [])
    if not visit_entries:
        errors["visit_seasons"] = "Select one season and service for each visit slot."
    if visit_entries and "visits_per_year" not in errors and len(visit_entries) != visits_per_year:
        errors["visit_seasons"] = "Select one season and service for each visit slot."
    for entry in visit_entries:
        if not isinstance(entry, dict):
            errors["visit_seasons"] = "Select one season and service for each visit slot."
            break
        season_value = str(entry.get("season") or "").strip()
        service_id_value = str(entry.get("service_id") or "").strip()
        service_name_value = str(entry.get("service_name") or "").strip()
        if not season_value or not service_id_value or not service_name_value:
            errors["visit_seasons"] = "Each visit slot requires a season and service."
            break
        if season_value == "Custom":
            start_date_value = str(entry.get("start_date") or "").strip()
            end_date_value = str(entry.get("end_date") or "").strip()
            if not start_date_value or not end_date_value:
                errors["visit_seasons"] = "Custom season slots require both start and end dates."
                break

    try:
        price_annual = float(payload.get("price_annual"))
        if price_annual <= 0:
            errors["price_annual"] = "Annual price must be greater than 0."
    except (TypeError, ValueError):
        if "price_annual" not in errors:
            errors["price_annual"] = "Annual price must be a number."

    try:
        discount_pct = float(payload.get("repair_discount_pct") or 0)
        if discount_pct < 0 or discount_pct > 50:
            errors["repair_discount_pct"] = "Repair discount must be between 0 and 50."
        elif discount_pct > 0:
            service_types = list(payload.get("discount_service_types") or [])
            line_item_types = list(payload.get("discount_line_item_types") or [])
            if not service_types:
                errors["repair_discount_pct"] = "Select at least one service type when discount is greater than 0."
            elif not line_item_types:
                errors["repair_discount_pct"] = "Select at least one line item type when discount is greater than 0."
    except (TypeError, ValueError):
        errors["repair_discount_pct"] = "Repair discount must be a number."

    business_scope = _maintenance_template_business_scope_predicate(business_id)

    exclude_predicate = None
    if isinstance(exclude_template_id, ObjectId):
        exclude_predicate = {"_id": {"$ne": exclude_template_id}}

    if tier_order is not None and "tier_order" not in errors:
        tier_query = {"$and": [business_scope, {"tier_order": tier_order}]}
        if exclude_predicate:
            tier_query["$and"].append(exclude_predicate)
        tier_conflict = db.maintenance_plan_templates.find_one(tier_query, {"_id": 1})
        if tier_conflict:
            errors["tier_order"] = (
                f"A plan already uses tier {tier_order}. Change that plan's tier order first."
            )

    name_value = str(payload.get("name") or "").strip()
    if name_value and "name" not in errors:
        escaped_name = re.escape(name_value)
        name_query = {
            "$and": [
                business_scope,
                {"name": {"$regex": f"^{escaped_name}$", "$options": "i"}},
            ]
        }
        if exclude_predicate:
            name_query["$and"].append(exclude_predicate)
        name_conflict = db.maintenance_plan_templates.find_one(name_query, {"_id": 1})
        if name_conflict:
            errors["name"] = "A plan template with this name already exists."

    return errors


def _build_renewal_count(db, business_id):
    business_scope = _maintenance_template_business_scope_predicate(business_id)
    renewal_count = 0
    today = datetime.now(UTC).date()
    cutoff = today + timedelta(days=60)
    now = datetime.now(UTC).replace(tzinfo=None)

    query = {
        "$and": [
            business_scope,
            {"status": "active"},
            {"renewal_date": {"$exists": True, "$ne": None}},
        ]
    }
    for plan in db.maintenance_plans.find(query, {"renewal_date": 1, "snoozed_until": 1}):
        if _maintenance_plan_is_snoozed(plan, now):
            continue
        renewal_value = plan.get("renewal_date")
        renewal_dt = _parse_datetime(renewal_value)
        if not renewal_dt and hasattr(renewal_value, "year") and hasattr(renewal_value, "month") and hasattr(renewal_value, "day"):
            try:
                renewal_dt = datetime(
                    renewal_value.year,
                    renewal_value.month,
                    renewal_value.day,
                )
            except Exception:
                renewal_dt = None

        if renewal_dt and today <= renewal_dt.date() <= cutoff:
            renewal_count += 1

    return renewal_count


MAINTENANCE_PLAN_STATUS_LABELS = {
    "active": "Active",
    "pending": "Pending",
    "lapsed": "Lapsed",
    "cancelled": "Cancelled",
    "expired": "Expired",
}


def _format_long_date(value):
    parsed = _parse_datetime(value)
    if not parsed and hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        try:
            parsed = datetime(value.year, value.month, value.day)
        except Exception:
            parsed = None
    if not parsed:
        return ""
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _maintenance_plan_is_snoozed(plan, now=None):
    snoozed_until = (plan or {}).get("snoozed_until")
    if not snoozed_until:
        return False
    if now is None:
        now = datetime.now(UTC).replace(tzinfo=None)
    snoozed_dt = _parse_datetime(snoozed_until)
    if not snoozed_dt:
        return False
    return snoozed_dt > now


def _build_maintenance_plan_summary_email(plan, customer, business):
    plan = plan or {}
    customer = customer or {}
    business = business or {}
    snapshot = plan.get("template_snapshot") or {}

    company_name = str(
        business.get("business_name")
        or business.get("company_name")
        or business.get("name")
        or "Your service team"
    ).strip()
    tier_name = str(snapshot.get("name") or "").strip() or "Maintenance Plan"
    first_name = str(customer.get("first_name") or "").strip() or "there"

    billing_type = str(plan.get("billing_type") or "").strip().lower()
    billing_label = "Monthly" if billing_type == "monthly" else "Annual"

    lines = [
        f"Hi {first_name},",
        "",
        f"Thank you for being a {company_name} maintenance plan member. "
        "Here is a summary of your plan.",
        "",
        f"Plan: {tier_name}",
    ]

    plan_number = str(plan.get("plan_number") or "").strip()
    if plan_number:
        lines.append(f"Plan Number: {plan_number}")

    property_name = str(plan.get("property_name") or "").strip()
    if property_name:
        lines.append(f"Property: {property_name}")

    start_display = _format_long_date(plan.get("start_date"))
    end_display = _format_long_date(plan.get("end_date"))
    if start_display and end_display:
        lines.append(f"Coverage Period: {start_display} - {end_display}")

    lines.append(f"Billing: {billing_label}")

    covered_systems = [
        system for system in (plan.get("covered_systems") or []) if isinstance(system, dict)
    ]
    if covered_systems:
        lines.append("")
        lines.append("Covered Systems:")
        for system in covered_systems:
            label = str(system.get("system_nickname") or system.get("system_type") or "System").strip()
            details = [
                part
                for part in [
                    str(system.get("system_type") or "").strip(),
                    str(system.get("manufacturer") or "").strip(),
                ]
                if part
            ]
            suffix = f" ({', '.join(details)})" if details else ""
            lines.append(f"  - {label}{suffix}")

    benefits = []
    try:
        repair_pct = float(snapshot.get("repair_discount_pct") or 0)
    except (TypeError, ValueError):
        repair_pct = 0
    if repair_pct > 0:
        benefits.append(f"{repair_pct:g}% repair discount")
    if snapshot.get("diagnostic_fee_waived"):
        benefits.append("Diagnostic fee waived")
    if snapshot.get("priority_scheduling"):
        benefits.append("Priority scheduling on all jobs")
    if snapshot.get("emergency_service"):
        benefits.append("After-hours emergency service included")
    for entry in snapshot.get("custom_benefits") or []:
        entry_text = str(entry or "").strip()
        if entry_text:
            benefits.append(entry_text)
    if benefits:
        lines.append("")
        lines.append("Your Plan Benefits:")
        for benefit in benefits:
            lines.append(f"  - {benefit}")

    company_phone = str(business.get("phone") or business.get("business_phone") or "").strip()
    lines.append("")
    if company_phone:
        lines.append(f"If you have any questions, please reach out to us at {company_phone}.")
    else:
        lines.append("If you have any questions, please reach out to us.")
    lines.append("")
    lines.append(f"Thank you,")
    lines.append(company_name)

    subject = f"Your {tier_name} Summary from {company_name}"
    return subject, "\n".join(lines)


def _build_maintenance_plan_renewal_email(plan, customer, business):
    plan = plan or {}
    customer = customer or {}
    business = business or {}
    snapshot = plan.get("template_snapshot") or {}

    company_name = str(
        business.get("business_name")
        or business.get("company_name")
        or business.get("name")
        or "Your service team"
    ).strip()
    tier_name = str(snapshot.get("name") or "").strip() or "Maintenance Plan"
    first_name = str(customer.get("first_name") or "").strip() or "there"

    end_display = _format_long_date(plan.get("end_date"))

    lines = [
        f"Hi {first_name},",
        "",
    ]

    if end_display:
        lines.append(
            f"Your {tier_name} with {company_name} is set to expire on {end_display}. "
            "Renew now to keep your coverage and benefits without interruption."
        )
    else:
        lines.append(
            f"Your {tier_name} with {company_name} is coming up for renewal. "
            "Renew now to keep your coverage and benefits without interruption."
        )

    property_name = str(plan.get("property_name") or "").strip()
    if property_name:
        lines.append("")
        lines.append(f"Property: {property_name}")

    benefits = []
    try:
        repair_pct = float(snapshot.get("repair_discount_pct") or 0)
    except (TypeError, ValueError):
        repair_pct = 0
    if repair_pct > 0:
        benefits.append(f"{repair_pct:g}% repair discount")
    if snapshot.get("diagnostic_fee_waived"):
        benefits.append("Diagnostic fee waived")
    if snapshot.get("priority_scheduling"):
        benefits.append("Priority scheduling on all jobs")
    if snapshot.get("emergency_service"):
        benefits.append("After-hours emergency service included")
    for entry in snapshot.get("custom_benefits") or []:
        entry_text = str(entry or "").strip()
        if entry_text:
            benefits.append(entry_text)
    if benefits:
        lines.append("")
        lines.append("By renewing, you keep these benefits:")
        for benefit in benefits:
            lines.append(f"  - {benefit}")

    company_phone = str(business.get("phone") or business.get("business_phone") or "").strip()
    lines.append("")
    if company_phone:
        lines.append(f"To renew or ask any questions, please reach out to us at {company_phone}.")
    else:
        lines.append("To renew or ask any questions, please reach out to us.")
    lines.append("")
    lines.append("Thank you,")
    lines.append(company_name)

    subject = f"Renew Your {tier_name} with {company_name}"
    return subject, "\n".join(lines)


def _build_plan_list_view(plan):
    snapshot = plan.get("template_snapshot") or {}
    status = str(plan.get("status") or "").strip().lower()
    billing_type = str(plan.get("billing_type") or "").strip().lower()
    end_date = plan.get("end_date")
    end_dt = _parse_datetime(end_date)
    visits_completed = int(plan.get("visits_completed") or 0)
    visits_scheduled = int(plan.get("visits_scheduled") or 0)
    next_visit_display = _format_long_date(plan.get("next_visit_date")) or "None"

    return {
        "plan_id": str(plan.get("_id")),
        "plan_number": str(plan.get("plan_number") or "").strip(),
        "customer_id": str(plan.get("customer_id") or "").strip(),
        "customer_name": str(plan.get("customer_name") or "").strip() or "Customer",
        "property_id": str(plan.get("property_id") or "").strip(),
        "property_name": str(plan.get("property_name") or "").strip() or "Property",
        "tier_name": str(snapshot.get("name") or "").strip() or "Maintenance Plan",
        "status": status,
        "status_label": MAINTENANCE_PLAN_STATUS_LABELS.get(status, status.capitalize() or "Unknown"),
        "billing_type_label": "Monthly" if billing_type == "monthly" else "Annual",
        "start_display": _format_long_date(plan.get("start_date")),
        "end_display": _format_long_date(end_date),
        "end_sort": end_dt.isoformat() if end_dt else "",
        "visits_display": f"{visits_completed} of {visits_scheduled}",
        "next_visit_display": next_visit_display,
    }


def _fetch_business_plans(db, business_id, status=None):
    predicates = [_maintenance_template_business_scope_predicate(business_id)]
    normalized_status = str(status or "all").strip().lower()
    if normalized_status in {"active", "pending", "lapsed", "cancelled", "expired"}:
        predicates.append({"status": normalized_status})

    query = {"$and": predicates} if len(predicates) > 1 else predicates[0]
    plans = list(db.maintenance_plans.find(query))

    def _end_sort_key(plan):
        end_dt = _parse_datetime(plan.get("end_date"))
        return end_dt or datetime.max

    plans.sort(key=_end_sort_key)
    return [_build_plan_list_view(plan) for plan in plans]


def _build_renewal_queue(db, business_id, days=90):
    business_scope = _maintenance_template_business_scope_predicate(business_id)
    today = datetime.now(UTC).date()
    cutoff = today + timedelta(days=days)
    now = datetime.now(UTC).replace(tzinfo=None)

    query = {
        "$and": [
            business_scope,
            {"status": "active"},
            {"renewal_date": {"$exists": True, "$ne": None}},
        ]
    }

    rows = []
    for plan in db.maintenance_plans.find(query):
        if _maintenance_plan_is_snoozed(plan, now):
            continue
        renewal_dt = _parse_datetime(plan.get("renewal_date"))
        if not renewal_dt:
            continue
        renewal_date = renewal_dt.date()
        if not (today <= renewal_date <= cutoff):
            continue

        snapshot = plan.get("template_snapshot") or {}
        days_remaining = (renewal_date - today).days
        rows.append(
            {
                "plan_id": str(plan.get("_id")),
                "customer_id": str(plan.get("customer_id") or "").strip(),
                "customer_name": str(plan.get("customer_name") or "").strip() or "Customer",
                "property_id": str(plan.get("property_id") or "").strip(),
                "property_name": str(plan.get("property_name") or "").strip() or "Property",
                "tier_name": str(snapshot.get("name") or "").strip() or "Maintenance Plan",
                "template_name": str(snapshot.get("name") or "").strip(),
                "expiry_display": _format_long_date(plan.get("renewal_date")),
                "days_remaining": days_remaining,
                "renewal_sort": renewal_dt.isoformat(),
            }
        )

    rows.sort(key=lambda row: row["renewal_sort"])
    return rows


# ---------------------------------------------------------------------------
# Maintenance Plan Templates — API routes
# ---------------------------------------------------------------------------

@bp.route("/api/maintenance-plan-templates", methods=["GET"])
def api_list_maintenance_plan_templates():
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return jsonify({"error": "Unauthorized"}), 403
    business_id = _resolve_current_business_ref(current_employee)
    if not business_id:
        return jsonify({"success": False, "error": "Business context unavailable"}), 403

    status_filter = str(request.args.get("status") or "all").strip().lower()
    if status_filter not in {"all", "active", "inactive"}:
        status_filter = "all"

    predicates = [_maintenance_template_business_scope_predicate(business_id)]
    if status_filter == "active":
        predicates.append({"is_active": True})
    elif status_filter == "inactive":
        predicates.append({"is_active": False})

    query = {"$and": predicates} if len(predicates) > 1 else predicates[0]
    docs = list(db.maintenance_plan_templates.find(query).sort("tier_order", 1))
    templates = [serialize_doc(doc) for doc in docs]
    return jsonify({"success": True, "templates": templates}), 200


@bp.route("/api/maintenance-plan-templates", methods=["POST"])
def api_create_maintenance_plan_template():
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return jsonify({"error": "Unauthorized"}), 403
    business_id = _resolve_current_business_ref(current_employee)
    if not business_id:
        return jsonify({"success": False, "error": "Business context unavailable"}), 403

    payload = request.get_json(silent=True) or {}
    errors = _validate_maintenance_template_payload(payload, db, business_id)
    if errors:
        tier_error = errors.get("tier_order")
        if tier_error and tier_error.startswith("A plan already uses tier"):
            return jsonify({"success": False, "error": tier_error, "errors": errors}), 400
        return jsonify({"success": False, "errors": errors, "error": "Validation failed"}), 400

    now = datetime.now(UTC)
    template_doc = _build_maintenance_plan_template_document(payload, business_id, now=now)
    template_doc["created_at"] = now
    template_doc["updated_at"] = now

    insert_result = db.maintenance_plan_templates.insert_one(template_doc)
    created_doc = db.maintenance_plan_templates.find_one({"_id": insert_result.inserted_id})
    return jsonify({"success": True, "template": serialize_doc(created_doc)}), 201


@bp.route("/api/maintenance-plan-templates/<template_id>", methods=["PUT"])
def api_update_maintenance_plan_template(template_id):
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return jsonify({"error": "Unauthorized"}), 403
    business_id = _resolve_current_business_ref(current_employee)
    if not business_id:
        return jsonify({"success": False, "error": "Business context unavailable"}), 403

    if not ObjectId.is_valid(str(template_id or "")):
        return jsonify({"success": False, "error": "Template not found"}), 404

    template_oid = ObjectId(str(template_id))
    existing_doc = db.maintenance_plan_templates.find_one(
        {"$and": [{"_id": template_oid}, _maintenance_template_business_scope_predicate(business_id)]}
    )
    if not existing_doc:
        return jsonify({"success": False, "error": "Template not found"}), 404

    payload = request.get_json(silent=True) or {}
    errors = _validate_maintenance_template_payload(payload, db, business_id, exclude_template_id=template_oid)
    if errors:
        tier_error = errors.get("tier_order")
        if tier_error and tier_error.startswith("A plan already uses tier"):
            return jsonify({"success": False, "error": tier_error, "errors": errors}), 400
        return jsonify({"success": False, "errors": errors, "error": "Validation failed"}), 400

    now = datetime.now(UTC)
    update_doc = _build_maintenance_plan_template_document(payload, business_id, now=now)
    update_doc.pop("created_at", None)
    update_doc["updated_at"] = now

    db.maintenance_plan_templates.update_one({"_id": template_oid}, {"$set": update_doc})
    refreshed = db.maintenance_plan_templates.find_one({"_id": template_oid})
    return jsonify({"success": True, "template": serialize_doc(refreshed)}), 200


@bp.route("/api/maintenance-plan-templates/<template_id>/status", methods=["PATCH"])
def api_patch_maintenance_plan_template_status(template_id):
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return jsonify({"error": "Unauthorized"}), 403
    business_id = _resolve_current_business_ref(current_employee)
    if not business_id:
        return jsonify({"success": False, "error": "Business context unavailable"}), 403

    if not ObjectId.is_valid(str(template_id or "")):
        return jsonify({"success": False, "error": "Template not found"}), 404

    payload = request.get_json(silent=True) or {}
    if "is_active" not in payload or not isinstance(payload.get("is_active"), bool):
        return jsonify({"success": False, "error": "Body must include boolean is_active."}), 400

    template_oid = ObjectId(str(template_id))
    match_query = {"$and": [{"_id": template_oid}, _maintenance_template_business_scope_predicate(business_id)]}
    existing_doc = db.maintenance_plan_templates.find_one(match_query, {"_id": 1})
    if not existing_doc:
        return jsonify({"success": False, "error": "Template not found"}), 404

    is_active = bool(payload.get("is_active"))
    db.maintenance_plan_templates.update_one(
        {"_id": template_oid},
        {
            "$set": {
                "is_active": is_active,
                "updated_at": datetime.now(UTC),
            }
        },
    )
    return jsonify({"success": True, "is_active": is_active}), 200


@bp.route("/api/maintenance-plan-templates/<template_id>/duplicate", methods=["POST"])
def api_duplicate_maintenance_plan_template(template_id):
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return jsonify({"error": "Unauthorized"}), 403
    business_id = _resolve_current_business_ref(current_employee)
    if not business_id:
        return jsonify({"success": False, "error": "Business context unavailable"}), 403

    if not ObjectId.is_valid(str(template_id or "")):
        return jsonify({"success": False, "error": "Template not found"}), 404

    template_oid = ObjectId(str(template_id))
    original = db.maintenance_plan_templates.find_one(
        {"$and": [{"_id": template_oid}, _maintenance_template_business_scope_predicate(business_id)]}
    )
    if not original:
        return jsonify({"success": False, "error": "Template not found"}), 404

    max_tier = 0
    for doc in db.maintenance_plan_templates.find(
        _maintenance_template_business_scope_predicate(business_id),
        {"tier_order": 1},
    ):
        try:
            max_tier = max(max_tier, int(doc.get("tier_order") or 0))
        except (TypeError, ValueError):
            continue

    now = datetime.now(UTC)
    duplicate_doc = dict(original)
    duplicate_doc.pop("_id", None)
    duplicate_doc["name"] = f"Copy of — {str(original.get('name') or '').strip()}"
    duplicate_doc["is_active"] = False
    duplicate_doc["tier_order"] = max_tier + 1
    duplicate_doc["created_at"] = now
    duplicate_doc["updated_at"] = now

    insert_result = db.maintenance_plan_templates.insert_one(duplicate_doc)
    created_doc = db.maintenance_plan_templates.find_one({"_id": insert_result.inserted_id})
    return jsonify({"success": True, "template": serialize_doc(created_doc)}), 201


@bp.route("/api/maintenance-plan-templates/<template_id>", methods=["DELETE"])
def api_delete_maintenance_plan_template(template_id):
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return jsonify({"error": "Unauthorized"}), 403
    business_id = _resolve_current_business_ref(current_employee)
    if not business_id:
        return jsonify({"success": False, "error": "Business context unavailable"}), 403

    if not ObjectId.is_valid(str(template_id or "")):
        return jsonify({"success": False, "error": "Template not found"}), 404

    template_oid = ObjectId(str(template_id))
    template_doc = db.maintenance_plan_templates.find_one(
        {"$and": [{"_id": template_oid}, _maintenance_template_business_scope_predicate(business_id)]},
        {"_id": 1},
    )
    if not template_doc:
        return jsonify({"success": False, "error": "Template not found"}), 404

    sold_plans_query = {
        "$and": [
            _maintenance_template_business_scope_predicate(business_id),
            {
                "$or": [
                    {"template_id": template_oid},
                    {"template_id": str(template_oid)},
                ]
            },
        ]
    }
    sold_plan_count = db.maintenance_plans.count_documents(sold_plans_query)
    if sold_plan_count > 0:
        return jsonify(
            {
                "success": False,
                "error": "This template has sold plans and cannot be deleted. Deactivate it instead.",
            }
        ), 400

    db.maintenance_plan_templates.delete_one({"_id": template_oid})
    return jsonify({"success": True}), 200


@bp.route("/reporting/revenue/data")
def reporting_revenue_data():
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return jsonify({"error": "Unauthorized"}), 403
    _raw_biz = (current_employee or {}).get("business")
    business_id = ObjectId(_raw_biz) if _raw_biz and ObjectId.is_valid(str(_raw_biz)) else None

    start_param = (request.args.get("start_date") or "").strip()
    end_param = (request.args.get("end_date") or "").strip()

    if not start_param or not end_param:
        return jsonify({"error": "start_date and end_date are required"}), 400

    try:
        start_dt = datetime.fromisoformat(start_param)
        end_dt = datetime.fromisoformat(end_param).replace(hour=23, minute=59, second=59)
    except ValueError:
        return jsonify({"error": "Invalid date format. Use ISO 8601 (YYYY-MM-DD)."}), 400

    data = _build_revenue_report_data(db, start_dt, end_dt, business_id=business_id)
    return jsonify(data)


@bp.route("/admin")
def admin():
    return render_template("admin/admin.html")


@bp.route("/admin/reference")
def reference():
    return render_template("admin/reference.html")


@bp.route("/admin/diagnostic-assistant")
def diagnostic_assistant():
    return render_template("admin/diagnostic_assistant.html")


@bp.route("/admin/pt-charts")
def pt_charts():
    return render_template("admin/pt_charts.html")


@bp.route("/admin/price-book/maintenance-plans")
def maintenance_plans():
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return redirect(url_for("auth.login"))

    business_id = _resolve_current_business_ref(current_employee)
    if not business_id:
        return redirect(url_for("auth.login"))

    section = str(request.args.get("section") or "templates").strip().lower()
    if section not in {"templates", "all", "renewals"}:
        section = "templates"

    status_filter = str(request.args.get("status") or "all").strip().lower()
    if status_filter not in {"all", "active", "inactive"}:
        status_filter = "all"

    template_predicates = [_maintenance_template_business_scope_predicate(business_id)]
    if status_filter == "active":
        template_predicates.append({"is_active": True})
    elif status_filter == "inactive":
        template_predicates.append({"is_active": False})

    templates_query = {"$and": template_predicates} if len(template_predicates) > 1 else template_predicates[0]
    template_docs = list(db.maintenance_plan_templates.find(templates_query).sort("tier_order", 1))
    plan_templates = [serialize_doc(doc) for doc in template_docs]

    templates_count = db.maintenance_plan_templates.count_documents(
        _maintenance_template_business_scope_predicate(business_id)
    )
    business_scope = _maintenance_template_business_scope_predicate(business_id)
    active_plans_count = db.maintenance_plans.count_documents(
        {"$and": [business_scope, {"status": "active"}]}
    )
    all_plans_count = db.maintenance_plans.count_documents(business_scope)
    renewal_count = _build_renewal_count(db, business_id)

    all_plans = _fetch_business_plans(db, business_id)
    renewal_plans = _build_renewal_queue(db, business_id, days=90)
    renewal_template_names = [
        str(doc.get("name") or "").strip()
        for doc in db.maintenance_plan_templates.find(business_scope, {"name": 1}).sort("tier_order", 1)
        if str(doc.get("name") or "").strip()
    ]

    return render_template(
        "admin/maintenance_plans.html",
        templates_count=templates_count,
        all_plans_count=all_plans_count,
        active_plans_count=active_plans_count,
        renewal_count=renewal_count,
        active_section=section,
        status_filter=status_filter,
        plan_templates=plan_templates,
        all_plans=all_plans,
        renewal_plans=renewal_plans,
        renewal_template_names=renewal_template_names,
    )


@bp.route("/admin/price-book/maintenance-plans/new", methods=["GET", "POST"])
def maintenance_plan_template_new():
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return redirect(url_for("auth.login"))

    business_id = _resolve_current_business_ref(current_employee)
    if not business_id:
        return redirect(url_for("auth.login"))

    services = _fetch_active_service_options(db, business_id)
    templates_count = db.maintenance_plan_templates.count_documents(
        _maintenance_template_business_scope_predicate(business_id)
    )
    active_plans_count = db.maintenance_plans.count_documents(
        {
            "$and": [
                _maintenance_template_business_scope_predicate(business_id),
                {"status": "active"},
            ]
        }
    )
    renewal_count = _build_renewal_count(db, business_id)

    if request.method == "POST":
        form_payload = _build_template_form_payload_from_request(request)
        form_payload["is_active"] = True
        errors = _validate_maintenance_template_payload(form_payload, db, business_id)
        if errors:
            return render_template(
                "admin/maintenance_plan_form.html",
                mode="create",
                template=_normalize_form_payload_for_template(form_payload, default_active=True),
                form_errors=errors,
                services=services,
                active_plan_count=None,
                templates_count=templates_count,
                active_plans_count=active_plans_count,
                renewal_count=renewal_count,
                active_section="templates",
            )

        now = datetime.now(UTC)
        template_doc = _build_maintenance_plan_template_document(form_payload, business_id, now=now)
        template_doc["created_at"] = now
        template_doc["updated_at"] = now
        db.maintenance_plan_templates.insert_one(template_doc)
        flash("Plan template created successfully.", "success")
        return redirect(url_for("admin_bp.maintenance_plans"))

    return render_template(
        "admin/maintenance_plan_form.html",
        mode="create",
        template=None,
        form_errors={},
        services=services,
        active_plan_count=None,
        templates_count=templates_count,
        active_plans_count=active_plans_count,
        renewal_count=renewal_count,
        active_section="templates",
    )


@bp.route("/admin/price-book/maintenance-plans/<template_id>/edit", methods=["GET", "POST"])
def maintenance_plan_template_edit(template_id):
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return redirect(url_for("auth.login"))

    business_id = _resolve_current_business_ref(current_employee)
    if not business_id:
        return redirect(url_for("auth.login"))

    scope_query = _maintenance_template_business_scope_predicate(business_id)
    if not ObjectId.is_valid(str(template_id or "")):
        return redirect(url_for("admin_bp.maintenance_plans"))

    template_doc = db.maintenance_plan_templates.find_one(
        {
            "$and": [
                {"_id": ObjectId(str(template_id))},
                scope_query,
            ]
        }
    )
    if not template_doc:
        return redirect(url_for("admin_bp.maintenance_plans"))

    templates_count = db.maintenance_plan_templates.count_documents(scope_query)
    active_plans_count = db.maintenance_plans.count_documents(
        {
            "$and": [
                scope_query,
                {"status": "active"},
            ]
        }
    )
    renewal_count = _build_renewal_count(db, business_id)
    active_plan_count = db.maintenance_plans.count_documents(
        {
            "$and": [
                scope_query,
                {
                    "$or": [
                        {"template_id": template_doc.get("_id")},
                        {"template_id": str(template_doc.get("_id"))},
                    ]
                },
                {"status": "active"},
            ]
        }
    )

    if request.method == "POST":
        form_payload = _build_template_form_payload_from_request(request)
        form_payload["is_active"] = bool(template_doc.get("is_active", True))
        errors = _validate_maintenance_template_payload(
            form_payload,
            db,
            business_id,
            exclude_template_id=template_doc.get("_id"),
        )
        if errors:
            form_model = _normalize_form_payload_for_template(form_payload, default_active=bool(template_doc.get("is_active", True)))
            form_model["_id"] = str(template_doc.get("_id"))
            return render_template(
                "admin/maintenance_plan_form.html",
                mode="edit",
                template=form_model,
                form_errors=errors,
                services=_fetch_active_service_options(db, business_id),
                active_plan_count=active_plan_count,
                templates_count=templates_count,
                active_plans_count=active_plans_count,
                renewal_count=renewal_count,
                active_section="templates",
            )

        now = datetime.now(UTC)
        updated_doc = _build_maintenance_plan_template_document(form_payload, business_id, now=now)
        updated_doc.pop("created_at", None)
        updated_doc["updated_at"] = now

        db.maintenance_plan_templates.update_one(
            {"_id": template_doc.get("_id")},
            {"$set": updated_doc},
        )
        flash("Plan template updated successfully.", "success")
        return redirect(url_for("admin_bp.maintenance_plans"))

    services = _fetch_active_service_options(db, business_id)

    return render_template(
        "admin/maintenance_plan_form.html",
        mode="edit",
        template=_normalize_template_for_form(template_doc),
        form_errors={},
        services=services,
        active_plan_count=active_plan_count,
        templates_count=templates_count,
        active_plans_count=active_plans_count,
        renewal_count=renewal_count,
        active_section="templates",
    )


@bp.route("/api/maintenance-plans", methods=["POST"])
def api_create_maintenance_plan():
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return jsonify({"success": False, "error": "Unauthorized"}), 403
    business_id = _resolve_current_business_ref(current_employee)
    if not business_id:
        return jsonify({"success": False, "error": "Business context unavailable"}), 403

    payload = request.get_json(silent=True) or {}

    # 1. Validate template belongs to this business.
    raw_template_id = str(payload.get("template_id") or "").strip()
    if not ObjectId.is_valid(raw_template_id):
        return jsonify({"success": False, "error": "A valid plan template is required."}), 400
    template_doc = db.maintenance_plan_templates.find_one(
        {"$and": [{"_id": ObjectId(raw_template_id)}, _maintenance_template_business_scope_predicate(business_id)]}
    )
    if not template_doc:
        return jsonify({"success": False, "error": "Plan template not found."}), 404

    # 2. Validate property belongs to the customer.
    raw_customer_id = str(payload.get("customer_id") or "").strip()
    if not ObjectId.is_valid(raw_customer_id):
        return jsonify({"success": False, "error": "A valid customer is required."}), 400
    customer = db.customers.find_one({"_id": ObjectId(raw_customer_id)})
    if not customer:
        return jsonify({"success": False, "error": "Customer not found."}), 404

    raw_property_id = str(payload.get("property_id") or "").strip()
    customer_property = _find_maintenance_plan_property(customer, raw_property_id)
    if not customer_property:
        return jsonify({"success": False, "error": "Property not found for this customer."}), 404

    # 3. Validate no existing active plan for this property.
    existing_active = db.maintenance_plans.count_documents(
        {"$and": [build_reference_filter("property_id", raw_property_id), {"status": "active"}]},
        limit=1,
    )
    if existing_active:
        return jsonify({"success": False, "error": "This property already has an active maintenance plan."}), 409

    # 4. Validate covered systems.
    covered_systems_input = payload.get("covered_systems")
    if not isinstance(covered_systems_input, list) or not covered_systems_input:
        return jsonify({"success": False, "error": "At least one system must be covered by the plan."}), 400

    # 5. Validate billing type.
    billing_type = str(payload.get("billing_type") or "").strip().lower()
    if billing_type not in {"monthly", "annual"}:
        return jsonify({"success": False, "error": "Billing type must be monthly or annual."}), 400

    # 6. Monthly billing requires a configured monthly price.
    price_monthly = template_doc.get("price_monthly")
    price_annual = template_doc.get("price_annual")
    if billing_type == "monthly" and price_monthly in (None, ""):
        return jsonify({"success": False, "error": "This plan does not support monthly billing."}), 400

    billing_amount = float(price_monthly or 0.0) if billing_type == "monthly" else float(price_annual or 0.0)

    now = datetime.now(UTC)

    try:
        visits_per_year = int(template_doc.get("visits_per_year") or 0)
    except (TypeError, ValueError):
        visits_per_year = 0

    template_snapshot = serialize_doc(copy.deepcopy(template_doc))

    customer_name = f"{str(customer.get('first_name') or '').strip()} {str(customer.get('last_name') or '').strip()}".strip()
    company = str(customer.get("company") or "").strip()
    property_name = str(customer_property.get("property_name") or "").strip()
    property_address = {
        "address_line_1": str(customer_property.get("address_line_1") or "").strip(),
        "address_line_2": str(customer_property.get("address_line_2") or "").strip() or None,
        "city": str(customer_property.get("city") or "").strip(),
        "state": str(customer_property.get("state") or "").strip(),
        "zip_code": str(customer_property.get("zip_code") or "").strip(),
    }

    sold_by_employee_id = str(session.get("employee_id") or "").strip()
    sold_by_name = f"{str(current_employee.get('first_name') or '').strip()} {str(current_employee.get('last_name') or '').strip()}".strip()

    builder_input = {
        "template_id": raw_template_id,
        "template_snapshot": template_snapshot,
        "customer_id": raw_customer_id,
        "customer_name": customer_name,
        "company": company,
        "property_id": raw_property_id,
        "property_name": property_name,
        "property_address": property_address,
        "covered_systems": covered_systems_input,
        "status": "active",
        "start_date": payload.get("start_date"),
        "billing_type": billing_type,
        "billing_amount": billing_amount,
        "next_billing_date": payload.get("start_date"),
        "visits_scheduled": visits_per_year,
        "visits_completed": 0,
        "series_ids": [],
        "sold_via": str(payload.get("sold_via") or "office").strip().lower() or "office",
        "sold_by_employee_id": sold_by_employee_id,
        "sold_by_name": sold_by_name,
        "auto_renew": payload.get("auto_renew", False),
    }

    plan_doc = _build_maintenance_plan_document(builder_input, business_id, now=now)
    insert_result = db.maintenance_plans.insert_one(plan_doc)
    plan_id = insert_result.inserted_id

    # Regenerate plan_number from the inserted _id (first 6 chars, uppercased).
    plan_number = f"MP-{str(plan_id)[:6].upper()}"
    db.maintenance_plans.update_one({"_id": plan_id}, {"$set": {"plan_number": plan_number}})
    plan_doc["plan_number"] = plan_number

    # Spawn one recurring_job_series per visit season in the template.
    start_date = plan_doc.get("start_date")
    end_date = plan_doc.get("end_date")
    end_date_str = end_date.strftime("%m/%d/%Y") if isinstance(end_date, datetime) else ""
    priority_scheduling = bool(template_doc.get("priority_scheduling"))
    covered_equipment = plan_doc.get("covered_systems")

    series_ids = []
    anchor_dates = []
    for sequence, season_entry in enumerate(template_doc.get("visit_seasons") or [], start=1):
        if not isinstance(season_entry, dict):
            continue
        season_value = str(season_entry.get("season") or "").strip()
        service_id = season_entry.get("service_id")
        service_name = str(season_entry.get("service_name") or "").strip()

        anchor_dt = _maintenance_visit_anchor_datetime(season_value, start_date)
        anchor_dates.append(anchor_dt)
        anchor_date_str = anchor_dt.strftime("%m/%d/%Y")

        series_doc = {
            "customer_id": reference_value(raw_customer_id),
            "customer_name": customer_name,
            "company": company,
            "property_id": plan_doc.get("property_id"),
            "property_name": property_name,
            "business_id": business_id,
            "job_type": service_name,
            "services": [{"service_id": service_id, "service_name": service_name}],
            "parts": [],
            "labors": [],
            "materials": [],
            "equipments": [],
            "discounts": [],
            "status": "Active",
            "frequency": "yearly",
            "anchor_date": anchor_date_str,
            "anchor_time": None,
            "end_type": "plan_year",
            "end_date": end_date_str,
            "next_occurrence_date": anchor_date_str,
            "last_generated_occurrence_index": 0,
            "total_amount": 0.0,
            "maintenance_plan_id": plan_id,
            "is_maintenance_plan_visit": True,
            "visit_season": season_value,
            "visit_sequence": sequence,
            "plan_visit_limit": visits_per_year,
            "plan_year_start": start_date,
            "plan_year_end": end_date,
            "covered_equipment": covered_equipment,
            "priority_scheduling": priority_scheduling,
            "visit_invoice_amount": 0.00,
            "created_at": now,
        }
        series_result = db.recurring_job_series.insert_one(series_doc)
        series_ids.append(series_result.inserted_id)

    next_visit_date = min(anchor_dates) if anchor_dates else None
    db.maintenance_plans.update_one(
        {"_id": plan_id},
        {"$set": {"series_ids": series_ids, "next_visit_date": next_visit_date, "updated_at": now}},
    )

    saved_plan = db.maintenance_plans.find_one({"_id": plan_id})
    return jsonify({"success": True, "plan_id": str(plan_id), "data": serialize_doc(saved_plan)}), 201


@bp.route("/api/maintenance-plans", methods=["GET"])
def api_list_maintenance_plans():
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return jsonify({"success": False, "error": "Unauthorized"}), 403
    business_id = _resolve_current_business_ref(current_employee)
    if not business_id:
        return jsonify({"success": False, "error": "Business context unavailable"}), 403

    status_filter = str(request.args.get("status") or "all").strip().lower()
    if status_filter not in {"all", "active", "pending", "lapsed", "cancelled", "expired"}:
        status_filter = "all"

    plans = _fetch_business_plans(db, business_id, status=status_filter)
    return jsonify({"success": True, "plans": plans}), 200


@bp.route("/api/maintenance-plans/<plan_id>")
def api_get_maintenance_plan(plan_id):
    return jsonify({"error": "Not implemented"}), 501


@bp.route("/api/maintenance-plans/<plan_id>/cancel", methods=["PATCH"])
def api_cancel_maintenance_plan(plan_id):
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return jsonify({"success": False, "error": "Unauthorized"}), 403
    business_id = _resolve_current_business_ref(current_employee)
    if not business_id:
        return jsonify({"success": False, "error": "Business context unavailable"}), 403

    if not ObjectId.is_valid(str(plan_id or "")):
        return jsonify({"success": False, "error": "Plan not found."}), 404

    plan_oid = ObjectId(str(plan_id))
    plan = db.maintenance_plans.find_one(
        {"$and": [{"_id": plan_oid}, _maintenance_template_business_scope_predicate(business_id)]}
    )
    if not plan:
        return jsonify({"success": False, "error": "Plan not found."}), 404

    status = str(plan.get("status") or "").strip().lower()
    if status in ("cancelled", "expired"):
        return jsonify({"success": False, "error": "This plan is already cancelled or expired."}), 409

    payload = request.get_json(silent=True) or {}
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        return jsonify({"success": False, "error": "A cancellation reason is required."}), 400

    now = datetime.now(UTC)
    db.maintenance_plans.update_one(
        {"_id": plan_oid},
        {
            "$set": {
                "status": "cancelled",
                "cancelled_at": now,
                "cancellation_reason": reason,
                "updated_at": now,
            }
        },
    )
    db.recurring_job_series.update_many(
        build_reference_filter("maintenance_plan_id", plan_oid),
        {"$set": {"status": "Cancelled"}},
    )
    return jsonify({"success": True})


@bp.route("/api/maintenance-plans/<plan_id>/renew", methods=["POST"])
def api_renew_maintenance_plan(plan_id):
    return jsonify({"error": "Not implemented"}), 501


@bp.route("/api/maintenance-plans/<plan_id>/send-summary-email", methods=["POST"])
def api_send_maintenance_plan_summary_email(plan_id):
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return jsonify({"success": False, "error": "Unauthorized"}), 403
    business_id = _resolve_current_business_ref(current_employee)
    if not business_id:
        return jsonify({"success": False, "error": "Business context unavailable"}), 403

    if not ObjectId.is_valid(str(plan_id or "")):
        return jsonify({"success": False, "error": "Plan not found."}), 404

    plan = db.maintenance_plans.find_one(
        {"$and": [{"_id": ObjectId(str(plan_id))}, _maintenance_template_business_scope_predicate(business_id)]},
    )
    if not plan:
        return jsonify({"success": False, "error": "Plan not found."}), 404

    customer = db.customers.find_one(build_reference_filter("_id", plan.get("customer_id"))) or {}
    recipient_email = str(customer.get("email") or "").strip()
    if not recipient_email:
        return jsonify({"success": False, "error": "This customer does not have an email address on file."}), 400

    business = db.businesses.find_one(build_reference_filter("_id", plan.get("business_id"))) or {}
    subject, body = _build_maintenance_plan_summary_email(plan, customer, business)

    try:
        send_email(subject=subject, recipients=[recipient_email], body=body, business=business)
    except Exception as exc:
        current_app.logger.warning("Maintenance plan summary email failed: %s", exc)
        return jsonify({"success": False, "error": "The summary email could not be sent. Please try again."}), 502

    db.maintenance_plans.update_one(
        {"_id": plan.get("_id")},
        {"$set": {"summary_email_sent_at": datetime.now(UTC), "updated_at": datetime.now(UTC)}},
    )
    return jsonify({"success": True}), 200


@bp.route("/api/maintenance-plans/<plan_id>/send-renewal-email", methods=["POST"])
def api_send_maintenance_plan_renewal_email(plan_id):
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return jsonify({"success": False, "error": "Unauthorized"}), 403
    business_id = _resolve_current_business_ref(current_employee)
    if not business_id:
        return jsonify({"success": False, "error": "Business context unavailable"}), 403

    if not ObjectId.is_valid(str(plan_id or "")):
        return jsonify({"success": False, "error": "Plan not found."}), 404

    plan = db.maintenance_plans.find_one(
        {"$and": [{"_id": ObjectId(str(plan_id))}, _maintenance_template_business_scope_predicate(business_id)]},
    )
    if not plan:
        return jsonify({"success": False, "error": "Plan not found."}), 404

    customer = db.customers.find_one(build_reference_filter("_id", plan.get("customer_id"))) or {}
    recipient_email = str(customer.get("email") or "").strip()
    if not recipient_email:
        return jsonify({"success": False, "error": "This customer does not have an email address on file."}), 400

    business = db.businesses.find_one(build_reference_filter("_id", plan.get("business_id"))) or {}
    subject, body = _build_maintenance_plan_renewal_email(plan, customer, business)

    try:
        send_email(subject=subject, recipients=[recipient_email], body=body, business=business)
    except Exception as exc:
        current_app.logger.warning("Maintenance plan renewal email failed: %s", exc)
        return jsonify({"success": False, "error": "The renewal email could not be sent. Please try again."}), 502

    db.maintenance_plans.update_one(
        {"_id": plan.get("_id")},
        {"$set": {"renewal_email_sent_at": datetime.now(UTC), "updated_at": datetime.now(UTC)}},
    )
    return jsonify({"success": True}), 200



@bp.route("/api/maintenance-plans/<plan_id>/dismiss-renewal", methods=["POST"])
def api_dismiss_maintenance_plan_renewal(plan_id):
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    if not current_employee:
        return jsonify({"success": False, "error": "Unauthorized"}), 403
    business_id = _resolve_current_business_ref(current_employee)
    if not business_id:
        return jsonify({"success": False, "error": "Business context unavailable"}), 403

    if not ObjectId.is_valid(str(plan_id or "")):
        return jsonify({"success": False, "error": "Plan not found."}), 404

    plan_oid = ObjectId(str(plan_id))
    plan = db.maintenance_plans.find_one(
        {"$and": [{"_id": plan_oid}, _maintenance_template_business_scope_predicate(business_id)]},
        {"_id": 1},
    )
    if not plan:
        return jsonify({"success": False, "error": "Plan not found."}), 404

    now = datetime.now(UTC)
    db.maintenance_plans.update_one(
        {"_id": plan_oid},
        {"$set": {"snoozed_until": now + timedelta(days=7), "updated_at": now}},
    )
    return jsonify({"success": True}), 200


@bp.route("/reporting")
def reporting():
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    _raw_biz = (current_employee or {}).get("business")
    business_oid = ObjectId(_raw_biz) if _raw_biz and ObjectId.is_valid(str(_raw_biz)) else None
    business_id = str(_raw_biz or "").strip()
    revenue_performance = _build_revenue_performance_report(db, business_id=business_oid)
    accounts_receivable = _build_accounts_receivable_summary(db, business_id=business_oid)
    customer_health = _build_customer_health_report(db)
    today = datetime.now().date()
    todays_jobs_overview = _build_daily_job_overview_report(db, today, business_id=business_id)
    yesterdays_jobs_overview = _build_daily_job_overview_report(db, today - timedelta(days=1), business_id=business_id)
    return render_template(
        "admin/reporting.html",
        report_links=REPORT_LINKS,
        active_report_slug="dashboard",
        reporting_view_title="Dashboard",
        reporting_view_subtitle="Business Performance Overview",
        dashboard_context_message="",
        accounts_receivable=accounts_receivable,
        revenue_performance=revenue_performance,
        customer_health=customer_health,
        todays_jobs_overview=todays_jobs_overview,
        yesterdays_jobs_overview=yesterdays_jobs_overview,
    )


@bp.route("/reporting/revenue")
def reporting_revenue():
    return render_template(
        "admin/reporting.html",
        report_links=REPORT_LINKS,
        active_report_slug="revenue",
        reporting_view_title="Revenue",
        reporting_view_subtitle="Revenue performance from completed jobs.",
        dashboard_context_message="",
        accounts_receivable=None,
        revenue_performance=None,
        customer_health=None,
        todays_jobs_overview=None,
        yesterdays_jobs_overview=None,
        show_revenue_report=True,
    )


@bp.route("/reporting/accounts-receivable")
def reporting_accounts_receivable():
    db = ensure_connection_or_500()
    current_employee = _get_current_employee(db)
    _raw_biz = (current_employee or {}).get("business")
    business_oid = ObjectId(_raw_biz) if _raw_biz and ObjectId.is_valid(str(_raw_biz)) else None
    accounts_receivable = _build_accounts_receivable_summary(db, business_id=business_oid)
    return render_template(
        "admin/reporting.html",
        report_links=REPORT_LINKS,
        active_report_slug="accounts-receivable",
        reporting_view_title="Accounts Receivable",
        reporting_view_subtitle="Customer performance and collection insights.",
        dashboard_context_message="",
        accounts_receivable=accounts_receivable,
        revenue_performance=None,
        customer_health=None,
        todays_jobs_overview=None,
        yesterdays_jobs_overview=None,
    )


@bp.route("/reporting/jobs")
def reporting_jobs():
    return render_template(
        "admin/reporting.html",
        report_links=REPORT_LINKS,
        active_report_slug="jobs",
        reporting_view_title="Jobs",
        reporting_view_subtitle="Section setup in progress.",
        dashboard_context_message="",
        accounts_receivable=None,
        revenue_performance=None,
        customer_health=None,
        todays_jobs_overview=None,
        yesterdays_jobs_overview=None,
    )


@bp.route("/reporting/customers")
def reporting_customers():
    return render_template(
        "admin/reporting.html",
        report_links=REPORT_LINKS,
        active_report_slug="customers",
        reporting_view_title="Customers",
        reporting_view_subtitle="Section setup in progress.",
        dashboard_context_message="",
        accounts_receivable=None,
        revenue_performance=None,
        customer_health=None,
        todays_jobs_overview=None,
        yesterdays_jobs_overview=None,
    )


@bp.route("/admin/subscription")
def subscription():
    db = ensure_connection_or_500()
    if not is_owner_position(session.get("employee_position")):
        abort(403)
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


@bp.route("/admin/subscription/manage")
def manage_subscription():
    db = ensure_connection_or_500()
    if not is_owner_position(session.get("employee_position")):
        abort(403)
    employee = _get_current_employee(db)
    if not employee:
        return redirect(url_for("auth.login"))

    subscription_doc = _get_subscription_document(db, employee)
    if not subscription_doc:
        return render_template(
            "admin/manage_subscription.html",
            subscription=None,
            subscription_issue=True,
        )

    subscription_data = _build_subscription_view_model(db, employee, subscription_doc)
    return render_template(
        "admin/manage_subscription.html",
        subscription=subscription_data,
        subscription_issue=False,
    )


@bp.route("/admin/subscription/cancel", methods=["GET", "POST"])
def cancel_subscription():
    db = ensure_connection_or_500()
    if not is_owner_position(session.get("employee_position")):
        abort(403)
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
                    "end_date": datetime.now(UTC),
                    "ended_at": datetime.now(UTC),
                }
            },
        )
        return redirect(url_for("admin_bp.subscription", cancelled="1"))

    return render_template("admin/cancel_subscription.html", subscription_issue=False)


@bp.route("/admin/subscription/reactivate")
def reactivate_subscription():
    if not is_owner_position(session.get("employee_position")):
        abort(403)
    return render_template("admin/reactivate_subscription.html")
