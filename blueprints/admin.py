from calendar import monthrange
from datetime import datetime, timedelta
import math

from bson import ObjectId
from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from mongo import ensure_connection_or_500
from utils.currency import currency_to_float
from utils.invoices import collect_invoice_items

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


def _job_completed_datetime(job):
    completed_at = _parse_datetime((job or {}).get("completed_at"))
    if completed_at:
        return completed_at
    return _parse_completed_datetime((job or {}).get("dateCompleted"))


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

    _rev_filter = {"status": {"$regex": "^Completed$", "$options": "i"}}
    if business_id:
        _rev_filter["business_id"] = business_id
    completed_jobs = db.jobs.find(
        _rev_filter,
        {"total": 1, "total_amount": 1, "dateCompleted": 1, "completed_at": 1},
    )

    for job in completed_jobs:
        completed_at = _job_completed_datetime(job)
        if not completed_at:
            continue
        completed_day = completed_at.date()
        amount = _coerce_float(job.get("total_amount")) if job.get("total_amount") is not None else currency_to_float(job.get("total"))

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
    _ar_filter = {"status": {"$regex": "^Completed$", "$options": "i"}}
    if business_id:
        _ar_filter["business_id"] = business_id
    completed_jobs = db.jobs.find(
        _ar_filter,
        {"customer_id": 1, "dateCompleted": 1, "completed_at": 1},
    )

    oldest_completed_by_customer = {}
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


def _build_revenue_report_data(db, start_dt, end_dt, business_id=None):
    """Build revenue metrics for a given datetime range."""
    yoy_start_dt = start_dt.replace(year=start_dt.year - 1)
    yoy_end_dt = end_dt.replace(year=end_dt.year - 1)

    today = datetime.utcnow().date()
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
            "total": 1,
            "completed_at": 1,
            "dateCompleted": 1,
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
            completed_at = _job_completed_datetime(job)
            if not completed_at:
                continue
            if range_start <= completed_at <= range_end:
                amount = _coerce_float(job.get("total_amount")) if job.get("total_amount") is not None else currency_to_float(job.get("total"))
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
    cur_month_total, _, _ = _sum_jobs_in_range(all_jobs, cur_month_start, datetime.utcnow())
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
        completed_at = _job_completed_datetime(job)
        if not completed_at or not (start_dt <= completed_at <= end_dt):
            continue
        job_amount = _coerce_float(job.get("total_amount")) if job.get("total_amount") is not None else currency_to_float(job.get("total"))
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
        completed_at = _job_completed_datetime(job)
        if not completed_at or not (start_dt <= completed_at <= end_dt):
            continue

        job_amount = _coerce_float(job.get("total_amount")) if job.get("total_amount") is not None else currency_to_float(job.get("total"))

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
    }


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
    return render_template(
        "admin/reporting.html",
        report_links=REPORT_LINKS,
        active_report_slug="accounts-receivable",
        reporting_view_title="Accounts Receivable",
        reporting_view_subtitle="Section setup in progress.",
        dashboard_context_message="",
        accounts_receivable=None,
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
                    "end_date": datetime.utcnow(),
                    "ended_at": datetime.utcnow(),
                }
            },
        )
        return redirect(url_for("admin_bp.subscription", cancelled="1"))

    return render_template("admin/cancel_subscription.html", subscription_issue=False)


@bp.route("/admin/subscription/reactivate")
def reactivate_subscription():
    return render_template("admin/reactivate_subscription.html")
