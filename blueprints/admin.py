from calendar import monthrange
from datetime import UTC, datetime, timedelta
import math
import re

from bson import ObjectId
from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from mongo import ensure_connection_or_500, serialize_doc
from utils.currency import currency_to_float

bp = Blueprint("admin_bp", __name__)

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

    query = {
        "$and": [
            business_scope,
            {"renewal_date": {"$exists": True, "$ne": None}},
        ]
    }
    for plan in db.maintenance_plans.find(query, {"renewal_date": 1}):
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
    if section not in {"templates", "active", "renewals"}:
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
    active_plans_count = db.maintenance_plans.count_documents(
        {
            "$and": [
                _maintenance_template_business_scope_predicate(business_id),
                {"status": "active"},
            ]
        }
    )
    renewal_count = _build_renewal_count(db, business_id)

    return render_template(
        "admin/maintenance_plans.html",
        templates_count=templates_count,
        active_plans_count=active_plans_count,
        renewal_count=renewal_count,
        active_section=section,
        status_filter=status_filter,
        plan_templates=plan_templates,
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
    return render_template("admin/reactivate_subscription.html")
