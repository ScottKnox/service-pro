from datetime import datetime
import atexit
import logging
import os
import re

from bson import ObjectId
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, redirect, render_template, request, send_file, session, url_for
from flask_mail import Mail
from flask_wtf.csrf import CSRFProtect
import stripe

from blueprints import register_blueprints
from config import (
    APP_ENV,
    get_mail_config,
    get_secret_key,
    scheduler_enabled_flag,
    scheduler_interval_minutes,
    validate_startup_config,
)
from mongo import build_reference_filter, ensure_connection_or_500, serialize_doc
from utils.currency import normalize_currency

validate_startup_config()

app = Flask(__name__)

# Session Configuration
_secret_key = get_secret_key()
app.secret_key = _secret_key

# Flask-Mail Configuration
_mail_config = get_mail_config()
app.config["MAIL_SERVER"] = _mail_config["MAIL_SERVER"]
app.config["MAIL_PORT"] = _mail_config["MAIL_PORT"]
app.config["MAIL_USE_TLS"] = _mail_config["MAIL_USE_TLS"]
app.config["MAIL_USERNAME"] = _mail_config["MAIL_USERNAME"]
app.config["MAIL_PASSWORD"] = _mail_config["MAIL_PASSWORD"]
app.config["MAIL_DEFAULT_SENDER"] = _mail_config["MAIL_DEFAULT_SENDER"]

mail = Mail(app)
csrf = CSRFProtect(app)
register_blueprints(app)

# Jinja2 template filters
app.jinja_env.filters['currency'] = lambda val: normalize_currency(val)

_invoice_reminder_scheduler = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
app.logger.info("Application environment mode: %s", APP_ENV)


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", error_message="The page you requested could not be found."), 404


@app.errorhandler(500)
def internal_error(e):
    app.logger.error("Internal server error: %s", e)
    return render_template("error.html", error_message="An internal server error occurred. Please try again later."), 500


@app.context_processor
def inject_header_business_name():
    employee_id = str(session.get("employee_id") or "").strip()
    if not employee_id or not ObjectId.is_valid(employee_id):
        return {"header_business_name": ""}

    try:
        db = ensure_connection_or_500()
        employee_doc = db.employees.find_one({"_id": ObjectId(employee_id)}, {"business": 1}) or {}
        raw_business_id = employee_doc.get("business")
        if not raw_business_id or not ObjectId.is_valid(str(raw_business_id)):
            return {"header_business_name": ""}

        business_doc = db.businesses.find_one(
            {"_id": ObjectId(str(raw_business_id))},
            {"company_name": 1, "business_name": 1, "name": 1},
        ) or {}
        header_business_name = str(
            business_doc.get("company_name")
            or business_doc.get("business_name")
            or business_doc.get("name")
            or ""
        ).strip()
        return {"header_business_name": header_business_name}
    except Exception:
        app.logger.exception("Failed to load header business name")
        return {"header_business_name": ""}


@app.before_request
def require_login():
    """Redirect unauthenticated users to login for all protected endpoints."""
    open_endpoints = {
        "auth.login",
        "auth.logout",
        "static",
        "home",
        "error_page",
        "privacy_policy",
        "terms_and_conditions",
        "download_invoice",
        "jobs.view_estimate",
        "jobs.view_invoice",
        "jobs.create_invoice_checkout_session",
        "jobs.accept_estimate",
        "jobs.decline_estimate",
        "stripe_webhook",
    }
    endpoint = request.endpoint
    if endpoint is None:
        return

    if endpoint not in open_endpoints:
        employee_id = session.get("employee_id")
        if not employee_id:
            return redirect(url_for("auth.login"))
        if not ObjectId.is_valid(employee_id):
            session.clear()
            return redirect(url_for("auth.login"))

    # For cancelled subscriptions, only block create/add actions — employees
    # can still browse existing data.
    _restricted_endpoints = {
        "customers.add_customer",
        "jobs.create_job",
        "jobs.create_estimate",
        "customers.add_equipment",       # Add HVAC System
        "customers.add_hvac_diagnostics",
        "employees.add_employee",
        "catalog.create_service",
        "catalog.create_part",
        "catalog.create_equipment",
    }
    if request.endpoint and request.endpoint in _restricted_endpoints:
        employee_id = session.get("employee_id")
        if employee_id and ObjectId.is_valid(employee_id):
            try:
                db = ensure_connection_or_500()
                employee = db.employees.find_one({"_id": ObjectId(employee_id)}, {"subscription_id": 1})
                if employee:
                    subscription_id = (employee.get("subscription_id") or "").strip()
                    if subscription_id:
                        sub = db.subscriptions.find_one(
                            {"subscription_id": subscription_id},
                            {"status": 1},
                        )
                        if sub and (sub.get("status") or "").strip().lower() == "cancelled":
                            return redirect(url_for("admin_bp.reactivate_subscription"))
            except Exception:
                pass


@app.route("/")
def home():
    is_logged_in = bool(session.get("employee_id"))

    if not is_logged_in:
        return redirect(url_for("auth.login"))

    db = ensure_connection_or_500()

    current_employee_name = (session.get("employee_name") or "").strip()
    current_employee_position = (session.get("employee_position") or "").strip().lower()
    if not current_employee_position:
        _session_employee_id = session.get("employee_id")
        if _session_employee_id and ObjectId.is_valid(_session_employee_id):
            _position_doc = db.employees.find_one({"_id": ObjectId(_session_employee_id)}, {"position": 1})
            current_employee_position = str((_position_doc or {}).get("position") or "").strip().lower()
            if current_employee_position:
                session["employee_position"] = current_employee_position
    normalized_current_employee_name = " ".join(current_employee_name.lower().split())

    requested_home_view = str(request.args.get("home_view") or "").strip().lower()
    if requested_home_view in {"my_day", "dispatch"}:
        session["home_view_mode"] = requested_home_view

    home_view_mode = str(session.get("home_view_mode") or "").strip().lower()
    if home_view_mode not in {"my_day", "dispatch"}:
        if current_employee_position in {"owner", "clerk"}:
            home_view_mode = "dispatch"
        else:
            home_view_mode = "my_day"
        session["home_view_mode"] = home_view_mode

    _employee_id = session.get("employee_id")
    _business_oid = None
    if _employee_id and ObjectId.is_valid(_employee_id):
        _emp_doc = db.employees.find_one({"_id": ObjectId(_employee_id)}, {"business": 1})
        _raw_biz = (_emp_doc or {}).get("business")
        if _raw_biz and ObjectId.is_valid(str(_raw_biz)):
            _business_oid = ObjectId(_raw_biz) if isinstance(_raw_biz, str) else _raw_biz
    _biz_filter = {"business_id": _business_oid} if _business_oid else {}

    def _parse_internal_note_date(note_entry):
        raw_date = str((note_entry or {}).get("date_written") or "").strip()
        if not raw_date:
            return datetime.min

        for date_format in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
            try:
                return datetime.strptime(raw_date, date_format)
            except ValueError:
                continue
        return datetime.min

    def _normalize_employee_key(name):
        raw_name = str(name or "").strip().lower()
        if not raw_name:
            return ""
        return re.sub(r"\s+", "-", raw_name)

    def _parse_event_datetime(value):
        if isinstance(value, datetime):
            return value

        raw = str(value or "").strip()
        if not raw:
            return None

        for date_format in (
            "%m/%d/%Y %H:%M:%S",
            "%m/%d/%Y %H:%M",
            "%m/%d/%Y",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(raw, date_format)
            except ValueError:
                continue

        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None

    def _parse_scheduled_datetime(job_doc):
        scheduled_at = _parse_event_datetime((job_doc or {}).get("scheduled_at"))
        if scheduled_at:
            return scheduled_at

        raw_date = str((job_doc or {}).get("scheduled_date") or "").strip()
        raw_time = str((job_doc or {}).get("scheduled_time") or "").strip()
        if raw_date and raw_time:
            for date_format in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
                try:
                    return datetime.strptime(f"{raw_date} {raw_time}", date_format)
                except ValueError:
                    continue

        if raw_date:
            parsed_date = _parse_event_datetime(raw_date)
            if parsed_date:
                return parsed_date

        return _parse_event_datetime((job_doc or {}).get("dateScheduled"))

    def _compose_address(job_doc):
        parts = [
            str((job_doc or {}).get("address_line_1") or "").strip(),
            str((job_doc or {}).get("city") or "").strip(),
            str((job_doc or {}).get("state") or "").strip(),
            str((job_doc or {}).get("zip_code") or "").strip(),
        ]
        return ", ".join([part for part in parts if part])

    def _parse_date_and_time(date_value, time_value=""):
        raw_date = str(date_value or "").strip()
        raw_time = str(time_value or "").strip()
        if not raw_date:
            return None

        if raw_time:
            for date_format in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
                try:
                    return datetime.strptime(f"{raw_date} {raw_time}", date_format)
                except ValueError:
                    continue

        return _parse_event_datetime(raw_date)

    def _job_date_to_iso(date_value):
        raw_date = str(date_value or "").strip()
        if not raw_date:
            return ""

        for date_format in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
            try:
                return datetime.strptime(raw_date, date_format).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return ""

    def _iso_date_to_mmddyyyy(date_value):
        raw_date = str(date_value or "").strip()
        if not raw_date:
            return ""
        try:
            return datetime.strptime(raw_date, "%Y-%m-%d").strftime("%m/%d/%Y")
        except ValueError:
            return ""

    def _parse_time_to_minutes(time_value):
        raw_time = str(time_value or "").strip()
        if not raw_time:
            return None

        for time_format in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
            try:
                parsed = datetime.strptime(raw_time.upper(), time_format)
                return parsed.hour * 60 + parsed.minute
            except ValueError:
                continue
        return None

    def _resolve_primary_service(job_doc):
        services = list((job_doc or {}).get("services") or [])
        if not services:
            return "", ""

        first_service = services[0] if isinstance(services[0], dict) else {}
        service_name = ""
        for field_name in ("type", "service_name", "name", "service_code", "code", "description"):
            candidate = str(first_service.get(field_name) or "").strip()
            if candidate:
                service_name = candidate
                break

        service_category = ""
        for field_name in ("category", "service_category"):
            candidate = str(first_service.get(field_name) or "").strip()
            if candidate:
                service_category = candidate
                break

        return service_name, service_category

    def _resolve_duration_minutes(job_doc):
        raw_job = job_doc or {}

        def _coerce_positive_number(value):
            try:
                parsed = float(str(value or "").strip())
            except (TypeError, ValueError):
                return 0.0
            return parsed if parsed > 0 else 0.0

        for field_name in ("estimated_duration_minutes", "duration_minutes"):
            value = _coerce_positive_number(raw_job.get(field_name))
            if value:
                return max(15, int(round(value)))

        for field_name in ("estimated_duration", "estimated_hours", "duration_hours"):
            value = _coerce_positive_number(raw_job.get(field_name))
            if value:
                # Values in these fields are treated as hours.
                return max(15, int(round(value * 60)))

        first_service = (list(raw_job.get("services") or []) or [{}])[0]
        if isinstance(first_service, dict):
            for field_name in ("estimated_duration_minutes", "duration_minutes"):
                value = _coerce_positive_number(first_service.get(field_name))
                if value:
                    return max(15, int(round(value)))
            for field_name in ("estimated_hours", "service_hours", "duration_hours"):
                value = _coerce_positive_number(first_service.get(field_name))
                if value:
                    return max(15, int(round(value * 60)))

        return 45

    def _resolve_job_created_datetime(job_doc):
        return (
            _parse_event_datetime((job_doc or {}).get("created_at"))
            or _parse_event_datetime((job_doc or {}).get("date_created"))
            or _parse_event_datetime((job_doc or {}).get("updated_at"))
            or datetime.now()
        )

    def _employee_belongs_to_business(employee_doc):
        if not _business_oid:
            return True
        if not isinstance(employee_doc, dict):
            return False

        for field_name in ("business", "business_id", "company_id"):
            value = employee_doc.get(field_name)
            if not value:
                continue
            if str(value) == str(_business_oid):
                return True
        return False

    def _derive_activity_event(job_doc):
        status_key = str((job_doc or {}).get("status") or "").strip().lower()
        if not status_key:
            return None

        assigned_employee = str((job_doc or {}).get("assigned_employee") or "").strip()
        if not assigned_employee:
            return None

        action_word = "updated"
        event_dt = None

        if status_key == "scheduled":
            action_word = "scheduled"
            event_dt = _parse_scheduled_datetime(job_doc)
        elif status_key == "en route":
            action_word = "went en route to"
            event_dt = _parse_event_datetime((job_doc or {}).get("en_route_at"))
        elif status_key == "started":
            action_word = "started"
            event_dt = _parse_event_datetime((job_doc or {}).get("started_at")) or _parse_event_datetime((job_doc or {}).get("dateStarted"))
        elif status_key in {"completed", "complete", "done"}:
            action_word = "completed"
            event_dt = _parse_event_datetime((job_doc or {}).get("completed_at")) or _parse_event_datetime((job_doc or {}).get("dateCompleted"))
        elif status_key == "paid":
            action_word = "received payment for"
            event_dt = _parse_event_datetime((job_doc or {}).get("paid_at")) or _parse_event_datetime((job_doc or {}).get("completed_at")) or _parse_event_datetime((job_doc or {}).get("dateCompleted"))
        elif status_key == "pending":
            action_word = "created"
            event_dt = _parse_event_datetime((job_doc or {}).get("created_at"))
        else:
            action_word = f"updated to {status_key}"
            event_dt = _parse_event_datetime((job_doc or {}).get("updated_at"))

        fallback_dt = _parse_event_datetime((job_doc or {}).get("updated_at")) or _parse_event_datetime((job_doc or {}).get("created_at"))
        if not event_dt:
            event_dt = fallback_dt
        if not event_dt:
            return None

        customer_name = str((job_doc or {}).get("customer_name") or "").strip() or "Unknown customer"
        job_title = str((job_doc or {}).get("job_type") or "").strip() or "service"
        event_address = _compose_address(job_doc)

        return {
            "event_iso": event_dt.isoformat(),
            "event_display": event_dt.strftime("%b %d, %Y %I:%M %p"),
            "event_date_key": event_dt.strftime("%Y-%m-%d"),
            "employee": assigned_employee,
            "employee_key": _normalize_employee_key(assigned_employee),
            "action": action_word,
            "job_title": job_title,
            "customer_name": customer_name,
            "customer_preposition": "from" if status_key == "paid" else "for",
            "status": str((job_doc or {}).get("status") or "").strip(),
            "job_id": str((job_doc or {}).get("_id") or "").strip(),
            "address": event_address,
        }

    def _derive_estimate_activity_event(estimate_doc):
        status_key = str((estimate_doc or {}).get("status") or "").strip().lower()
        if status_key != "accepted":
            return None

        assigned_employee = str((estimate_doc or {}).get("estimated_by_employee") or "").strip()
        if not assigned_employee:
            return None

        event_dt = (
            _parse_event_datetime((estimate_doc or {}).get("accepted_signature_captured_at"))
            or _parse_date_and_time((estimate_doc or {}).get("date_accepted"), (estimate_doc or {}).get("time_accepted"))
            or _parse_event_datetime((estimate_doc or {}).get("updated_at"))
        )
        if not event_dt:
            return None

        customer_name = str((estimate_doc or {}).get("customer_name") or "").strip() or "Unknown customer"
        job_title = str(((estimate_doc or {}).get("services") or [{}])[0].get("type") or (estimate_doc or {}).get("property_name") or "estimate").strip() or "estimate"
        event_address = _compose_address(estimate_doc)

        return {
            "event_iso": event_dt.isoformat(),
            "event_display": event_dt.strftime("%b %d, %Y %I:%M %p"),
            "event_date_key": event_dt.strftime("%Y-%m-%d"),
            "employee": assigned_employee,
            "employee_key": _normalize_employee_key(assigned_employee),
            "action": "had estimate accepted for",
            "job_title": job_title,
            "customer_name": customer_name,
            "customer_preposition": "from",
            "status": str((estimate_doc or {}).get("status") or "").strip(),
            "job_id": str((estimate_doc or {}).get("created_job_id") or "").strip(),
            "estimate_id": str((estimate_doc or {}).get("_id") or "").strip(),
            "address": event_address,
        }

    jobs_list = []
    for job in db.jobs.find(_biz_filter).sort([("scheduled_date", 1), ("scheduled_time", 1), ("date_created", -1)]):
        serialized_job = serialize_doc(job)
        customer_phone = "N/A"
        customer_id = serialized_job.get("customer_id")
        if customer_id:
            customer_doc = db.customers.find_one(build_reference_filter("_id", customer_id), {"phone": 1})
            if customer_doc:
                customer_phone = (customer_doc.get("phone") or "").strip() or "N/A"

        latest_note_text = "No internal note yet."
        internal_notes = serialized_job.get("internal_notes")
        if isinstance(internal_notes, list) and internal_notes:
            valid_notes = [note for note in internal_notes if isinstance(note, dict)]
            if valid_notes:
                latest_note = max(valid_notes, key=_parse_internal_note_date)
                latest_note_text = str(latest_note.get("text") or "").strip() or "No internal note yet."

        serialized_job["customer_phone"] = customer_phone
        serialized_job["last_internal_note"] = latest_note_text
        jobs_list.append(serialized_job)
    employees = [
        serialize_doc(employee)
        for employee in db.employees.find().sort([("last_name", 1), ("first_name", 1)])
    ]
    employee_filters = []
    for employee in employees:
        full_name = f"{employee.get('first_name', '').strip()} {employee.get('last_name', '').strip()}".strip()
        if not full_name:
            continue
        normalized_full_name = " ".join(full_name.lower().split())
        employee_filters.append(
            {
                "label": full_name,
                "value": full_name.lower().replace(" ", "-"),
                "checked": current_employee_position == "clerk" or normalized_full_name == normalized_current_employee_name,
            }
        )

    # Activity Center payload (latest status activity feed + employee locations)
    activity_events = []
    for job in db.jobs.find(_biz_filter):
        event = _derive_activity_event(job)
        if event:
            activity_events.append(event)
    for estimate in db.estimates.find(_biz_filter):
        event = _derive_estimate_activity_event(estimate)
        if event:
            activity_events.append(event)
    activity_events.sort(key=lambda entry: entry.get("event_iso") or "", reverse=True)

    business_center_address = ""
    if _business_oid:
        business_doc = db.businesses.find_one(
            {"_id": _business_oid},
            {"address_line_1": 1, "city": 1, "state": 1, "zip_code": 1},
        )
        if business_doc:
            business_center_address = ", ".join(
                [
                    part
                    for part in [
                        str(business_doc.get("address_line_1") or "").strip(),
                        str(business_doc.get("city") or "").strip(),
                        str(business_doc.get("state") or "").strip(),
                        str(business_doc.get("zip_code") or "").strip(),
                    ]
                    if part
                ]
            )

    dispatch_selected_date = str(request.args.get("dispatch_date") or "").strip()
    if not dispatch_selected_date:
        dispatch_selected_date = datetime.now().strftime("%Y-%m-%d")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", dispatch_selected_date):
        dispatch_selected_date = datetime.now().strftime("%Y-%m-%d")

    dispatch_day_start_hour = 7
    dispatch_day_end_hour = 21
    dispatch_default_visible_start_hour = 7
    dispatch_day_start_minutes = dispatch_day_start_hour * 60
    dispatch_day_end_minutes = dispatch_day_end_hour * 60
    dispatch_default_visible_start_minutes = dispatch_default_visible_start_hour * 60

    employees_by_id = {}
    employee_name_to_id = {}
    dispatch_technicians = []
    for employee in employees:
        if not _employee_belongs_to_business(employee):
            continue

        employee_id = str(employee.get("_id") or "").strip()
        if not employee_id:
            continue

        full_name = f"{str(employee.get('first_name') or '').strip()} {str(employee.get('last_name') or '').strip()}".strip()
        if not full_name:
            continue

        employees_by_id[employee_id] = {
            "id": employee_id,
            "name": full_name,
            "status": str(employee.get("status") or "active").strip().lower(),
        }
        employee_name_to_id[" ".join(full_name.lower().split())] = employee_id

    for employee_info in employees_by_id.values():
        if employee_info["status"] == "active":
            dispatch_technicians.append({"id": employee_info["id"], "name": employee_info["name"]})
    dispatch_technicians.sort(key=lambda row: row["name"].lower())

    pending_jobs_dispatch = []
    scheduled_jobs_dispatch = []

    for job in sorted(jobs_list, key=lambda row: (_resolve_job_created_datetime(row), str(row.get("_id") or ""))):
        job_id = str(job.get("_id") or "").strip()
        if not job_id:
            continue

        primary_service_name, primary_service_category = _resolve_primary_service(job)
        job_address = _compose_address(job)
        created_dt = _resolve_job_created_datetime(job)
        pending_days = max(0, (datetime.now() - created_dt).days)

        primary_technician_id = str(job.get("primary_technician_id") or "").strip()
        scheduled_date_iso = _job_date_to_iso(job.get("scheduled_date"))
        scheduled_time_raw = str(job.get("scheduled_time") or "").strip()

        missing_primary_technician = not primary_technician_id
        missing_scheduled_date = not scheduled_date_iso
        if missing_primary_technician or missing_scheduled_date:
            additional_services = []
            for service in list(job.get("services") or [])[1:]:
                if not isinstance(service, dict):
                    continue
                label = ""
                for field_name in ("type", "service_name", "name", "service_code", "code", "description"):
                    candidate = str(service.get(field_name) or "").strip()
                    if candidate:
                        label = candidate
                        break
                if label:
                    additional_services.append(label)

            pending_jobs_dispatch.append(
                {
                    "id": job_id,
                    "customer_name": str(job.get("customer_name") or "Unknown Customer").strip() or "Unknown Customer",
                    "primary_service_name": primary_service_name,
                    "primary_service_category": primary_service_category,
                    "pending_days": pending_days,
                    "address": job_address,
                    "status": str(job.get("status") or "").strip(),
                    "scheduled_date": scheduled_date_iso,
                    "scheduled_time": scheduled_time_raw,
                    "primary_technician_id": primary_technician_id,
                    "assigned_employee": str(job.get("assigned_employee") or "").strip(),
                    "additional_services": additional_services,
                    "view_url": url_for("jobs.view_job", jobId=job_id),
                }
            )

        if not primary_technician_id or not scheduled_date_iso:
            continue

        schedule_minutes = _parse_time_to_minutes(scheduled_time_raw)
        if schedule_minutes is None:
            continue

        assigned_name = str(job.get("assigned_employee") or "").strip()
        if not primary_technician_id and assigned_name:
            primary_technician_id = employee_name_to_id.get(" ".join(assigned_name.lower().split()), "")

        scheduled_jobs_dispatch.append(
            {
                "id": job_id,
                "customer_name": str(job.get("customer_name") or "Unknown Customer").strip() or "Unknown Customer",
                "primary_service_name": primary_service_name,
                "primary_service_category": primary_service_category,
                "status": str(job.get("status") or "Pending").strip() or "Pending",
                "address": job_address,
                "date_iso": scheduled_date_iso,
                "scheduled_time": scheduled_time_raw,
                "start_minutes": schedule_minutes,
                "duration_minutes": _resolve_duration_minutes(job),
                "primary_technician_id": primary_technician_id,
                "assigned_employee": assigned_name,
                "view_url": url_for("jobs.view_job", jobId=job_id),
                "all_service_names": [
                    str(service.get("type") or service.get("service_name") or service.get("name") or "").strip()
                    for service in list(job.get("services") or [])
                    if isinstance(service, dict)
                ],
            }
        )

    pending_jobs_dispatch.sort(key=lambda row: (row["pending_days"], row["customer_name"].lower()))

    dispatch_tech_rows = {}
    for technician in dispatch_technicians:
        dispatch_tech_rows[technician["id"]] = {
            "id": technician["id"],
            "name": technician["name"],
            "is_active": True,
        }

    for job in scheduled_jobs_dispatch:
        technician_id = str(job.get("primary_technician_id") or "").strip()
        if not technician_id:
            assigned_name = str(job.get("assigned_employee") or "").strip()
            if assigned_name:
                technician_id = employee_name_to_id.get(" ".join(assigned_name.lower().split()), "")
                job["primary_technician_id"] = technician_id
        if not technician_id:
            continue

        if technician_id in dispatch_tech_rows:
            continue

        fallback_name = ""
        if technician_id in employees_by_id:
            fallback_name = employees_by_id[technician_id]["name"]
        if not fallback_name:
            fallback_name = str(job.get("assigned_employee") or "").strip() or "Former Technician"

        dispatch_tech_rows[technician_id] = {
            "id": technician_id,
            "name": fallback_name,
            "is_active": False,
        }

    dispatch_tech_rows_list = sorted(
        dispatch_tech_rows.values(),
        key=lambda row: (0 if row.get("is_active") else 1, str(row.get("name") or "").lower()),
    )

    if home_view_mode == "dispatch":
        return render_template(
            "dispatch_home.html",
            is_logged_in=True,
            home_view_mode=home_view_mode,
            dispatch_selected_date=dispatch_selected_date,
            dispatch_day_start_hour=dispatch_day_start_hour,
            dispatch_day_end_hour=dispatch_day_end_hour,
            dispatch_day_start_minutes=dispatch_day_start_minutes,
            dispatch_day_end_minutes=dispatch_day_end_minutes,
            dispatch_default_visible_start_minutes=dispatch_default_visible_start_minutes,
            dispatch_pending_jobs=pending_jobs_dispatch,
            dispatch_scheduled_jobs=scheduled_jobs_dispatch,
            dispatch_technicians=dispatch_technicians,
            dispatch_tech_rows=dispatch_tech_rows_list,
            dispatch_customers_url=url_for("customers.customers"),
            activity_events=activity_events,
            business_center_address=business_center_address,
            google_maps_api_key=(os.getenv("GOOGLE_MAPS_API_KEY") or "").strip(),
        )

    pending_page_raw = request.args.get("pending_page", "1")
    try:
        pending_page = max(1, int(pending_page_raw))
    except ValueError:
        pending_page = 1

    pending_jobs_per_page = 5
    pending_jobs_all = []
    for job in db.jobs.find({**_biz_filter, "status": {"$regex": "^Pending$", "$options": "i"}}).sort([("created_at", -1), ("_id", -1)]):
        serialized_job = serialize_doc(job)
        customer_phone = "N/A"
        customer_id = serialized_job.get("customer_id")
        if customer_id:
            customer_doc = db.customers.find_one(build_reference_filter("_id", customer_id), {"phone": 1})
            if customer_doc:
                customer_phone = (customer_doc.get("phone") or "").strip() or "N/A"

        serialized_job["customer_phone"] = customer_phone
        pending_jobs_all.append(serialized_job)

    pending_total_pages = (len(pending_jobs_all) + pending_jobs_per_page - 1) // pending_jobs_per_page
    if pending_total_pages == 0:
        pending_page = 1
        pending_jobs = []
    else:
        if pending_page > pending_total_pages:
            pending_page = pending_total_pages
        pending_start = (pending_page - 1) * pending_jobs_per_page
        pending_end = pending_start + pending_jobs_per_page
        pending_jobs = pending_jobs_all[pending_start:pending_end]

    # Notes section (Internal notes for currently active job)
    started_job_for_notes = None
    started_job_doc = db.jobs.find_one(
        {**_biz_filter, "status": {"$regex": "^(En Route|Started)$", "$options": "i"}},
        sort=[("_id", -1)],
    )
    if started_job_doc:
        started_job_for_notes = serialize_doc(started_job_doc)

    notes_all = []
    if started_job_for_notes:
        raw_notes = started_job_for_notes.get("internal_notes")
        if isinstance(raw_notes, list):
            valid_notes = [note for note in raw_notes if isinstance(note, dict)]
            valid_notes.sort(key=_parse_internal_note_date, reverse=True)
            for note in valid_notes:
                notes_all.append(
                    {
                        "text": str(note.get("text") or "").strip() or "-",
                        "date_written": str(note.get("date_written") or "").strip() or "",
                    }
                )

    notes_page_raw = request.args.get("notes_page", "1")
    try:
        notes_page = max(1, int(notes_page_raw))
    except ValueError:
        notes_page = 1

    notes_per_page = 5
    notes_total_pages = (len(notes_all) + notes_per_page - 1) // notes_per_page
    if notes_total_pages == 0:
        notes_page = 1
        notes = []
    else:
        if notes_page > notes_total_pages:
            notes_page = notes_total_pages
        notes_start = (notes_page - 1) * notes_per_page
        notes_end = notes_start + notes_per_page
        notes = notes_all[notes_start:notes_end]

    # Customer notes section (internal notes for customer on active job)
    customer_notes_all = []
    active_customer_for_notes = None
    if started_job_for_notes:
        active_customer_id = str(started_job_for_notes.get("customer_id") or "").strip()
        if active_customer_id:
            active_customer_doc = db.customers.find_one(
                build_reference_filter("_id", active_customer_id),
                {"notes": 1, "first_name": 1, "last_name": 1},
            ) or {}

            active_customer_for_notes = {
                "id": active_customer_id,
                "name": (
                    f"{str(active_customer_doc.get('first_name') or '').strip()} "
                    f"{str(active_customer_doc.get('last_name') or '').strip()}"
                ).strip()
                or "Customer",
            }

            raw_customer_notes = active_customer_doc.get("notes")
            if isinstance(raw_customer_notes, list):
                valid_customer_notes = [note for note in raw_customer_notes if isinstance(note, dict)]
                valid_customer_notes.sort(key=_parse_internal_note_date, reverse=True)
                for note in valid_customer_notes:
                    customer_notes_all.append(
                        {
                            "text": str(note.get("text") or "").strip() or "-",
                            "date_written": str(note.get("date_written") or "").strip() or "",
                        }
                    )

    customer_notes_page_raw = request.args.get("customer_notes_page", "1")
    try:
        customer_notes_page = max(1, int(customer_notes_page_raw))
    except ValueError:
        customer_notes_page = 1

    customer_notes_per_page = 5
    customer_notes_total_pages = (len(customer_notes_all) + customer_notes_per_page - 1) // customer_notes_per_page
    if customer_notes_total_pages == 0:
        customer_notes_page = 1
        customer_notes = []
    else:
        if customer_notes_page > customer_notes_total_pages:
            customer_notes_page = customer_notes_total_pages
        customer_notes_start = (customer_notes_page - 1) * customer_notes_per_page
        customer_notes_end = customer_notes_start + customer_notes_per_page
        customer_notes = customer_notes_all[customer_notes_start:customer_notes_end]

    # Current Property (HVAC Systems for Active Job)
    hvac_systems_payload = []
    current_property = None
    active_job = None
    for job in db.jobs.find(
        {
            **_biz_filter,
            "status": {"$regex": "^(En Route|Started)$", "$options": "i"},
        }
    ):
        started_job = serialize_doc(job)
        if started_job:
            active_job = started_job
            break

    if active_job:
        current_property_id = str(active_job.get("property_id") or "").strip()
        customer_id_ref = active_job.get("customer_id")

        if current_property_id and customer_id_ref:
            customer_id_oid = ObjectId(customer_id_ref) if isinstance(customer_id_ref, str) and ObjectId.is_valid(customer_id_ref) else customer_id_ref
            if customer_id_oid:
                customer_doc = db.customers.find_one({"_id": customer_id_oid}, {"properties": 1})
                if customer_doc and isinstance(customer_doc.get("properties"), list):
                    for prop in customer_doc.get("properties", []):
                        if str(prop.get("property_id") or "") == current_property_id:
                            current_property = serialize_doc(prop)
                            break

    hvac_systems_all = []
    if active_job and current_property:
        _cid_raw = active_job.get("customer_id")
        customer_id_oid = ObjectId(_cid_raw) if isinstance(_cid_raw, str) and ObjectId.is_valid(_cid_raw) else _cid_raw
        customer_id_text = str(_cid_raw or "").strip()
        customer_id_candidates = []
        if customer_id_oid is not None:
            customer_id_candidates.append(customer_id_oid)
        if customer_id_text:
            customer_id_candidates.append(customer_id_text)
        property_id_str = str(current_property.get("property_id") or "").strip()

        if customer_id_candidates:
            hvac_systems_cursor = db.hvacSystems.find({"customer_id": {"$in": customer_id_candidates}})
        else:
            hvac_systems_cursor = []

        for hvac_sys in hvac_systems_cursor:
            serialized_sys = serialize_doc(hvac_sys)
            sys_property_id = str(serialized_sys.get("property_id") or "").strip()
            
            if sys_property_id != property_id_str:
                continue

            sys_id = str(serialized_sys.get("_id") or "").strip()
            sys_type = str(serialized_sys.get("system_type") or "HVAC System").strip()
            system_tonnage = str(serialized_sys.get("system_tonnage") or "").strip() or "-"
            cooling_capacity = str(serialized_sys.get("cooling_capacity") or "").strip() or "-"
            heating_capacity = str(serialized_sys.get("heating_capacity") or "").strip() or "-"

            refrigerant_type = "-"
            hvac_system_id_candidates = [sys_id]
            if ObjectId.is_valid(sys_id):
                hvac_system_id_candidates.append(ObjectId(sys_id))

            refrigerant_query = {
                "hvac_system_id": {"$in": hvac_system_id_candidates},
            }
            if customer_id_candidates:
                refrigerant_query["customer_id"] = {"$in": customer_id_candidates}

            refrigerant_doc = db.refrigerants.find_one(refrigerant_query)
            if refrigerant_doc:
                refrigerant_type = str(refrigerant_doc.get("refrigerant_type") or "").strip() or "-"

            hvac_systems_all.append({
                "id": sys_id,
                "type": sys_type,
                "tonnage": system_tonnage,
                "cooling_capacity": cooling_capacity,
                "heating_capacity": heating_capacity,
                "refrigerant_type": refrigerant_type,
                "customer_id": str(customer_id_oid),
                "property_id": property_id_str,
            })

    hvac_page_raw = request.args.get("hvac_page", "1")
    try:
        hvac_page = max(1, int(hvac_page_raw))
    except ValueError:
        hvac_page = 1

    hvac_per_page = 5
    hvac_total_pages = (len(hvac_systems_all) + hvac_per_page - 1) // hvac_per_page if hvac_systems_all else 0
    if hvac_total_pages == 0:
        hvac_page = 1
        hvac_systems_payload = []
    else:
        if hvac_page > hvac_total_pages:
            hvac_page = hvac_total_pages
        hvac_start = (hvac_page - 1) * hvac_per_page
        hvac_end = hvac_start + hvac_per_page
        hvac_systems_payload = hvac_systems_all[hvac_start:hvac_end]

    # Price Book section (components on currently started job)
    def _first_non_empty(values, fallback="-"):
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return fallback

    def _resolve_price_book_view_url(item_type, item):
        endpoint_by_type = {
            "service": ("catalog.view_service", "serviceId"),
            "part": ("catalog.view_part", "partId"),
            "equipment": ("catalog.view_equipment", "equipmentId"),
            "labor": ("catalog.view_labor", "laborId"),
            "material": ("catalog.view_material", "materialId"),
        }

        collection_by_type = {
            "service": "services",
            "part": "parts",
            "equipment": "equipment",
            "labor": "labors",
            "material": "materials",
        }

        lookup_fields_by_type = {
            "service": ["code", "service_code", "name", "service_name", "type", "description"],
            "part": ["code", "part_code", "name", "part_name", "part_number", "description"],
            "equipment": ["equipment_name", "name", "sku", "description"],
            "labor": ["description", "labor_description", "labor_name", "name"],
            "material": ["material_name", "name", "part_number", "description"],
        }

        endpoint_info = endpoint_by_type.get(item_type)
        collection_name = collection_by_type.get(item_type)
        lookup_fields = lookup_fields_by_type.get(item_type) or []
        if not endpoint_info or not collection_name:
            return ""

        endpoint_name, endpoint_arg = endpoint_info

        raw_id = str(item.get("_id") or "").strip()
        if raw_id and ObjectId.is_valid(raw_id):
            return url_for(endpoint_name, **{endpoint_arg: raw_id})

        lookup_candidates = []
        for field_name in lookup_fields:
            field_value = str(item.get(field_name) or "").strip()
            if field_value:
                lookup_candidates.append((field_name, field_value))

        if not lookup_candidates:
            return ""

        collection = getattr(db, collection_name)
        for field_name, field_value in lookup_candidates:
            lookup_query = {**_biz_filter, field_name: field_value}
            matched_doc = collection.find_one(lookup_query, {"_id": 1})
            if matched_doc and matched_doc.get("_id"):
                return url_for(endpoint_name, **{endpoint_arg: str(matched_doc["_id"])})

        return ""

    started_job_for_price_book = None
    started_job_for_price_book_doc = db.jobs.find_one(
        {**_biz_filter, "status": {"$regex": "^Started$", "$options": "i"}},
        sort=[("_id", -1)],
    )
    if started_job_for_price_book_doc:
        started_job_for_price_book = serialize_doc(started_job_for_price_book_doc)
    price_book_items_all = []
    if started_job_for_price_book:
        services = started_job_for_price_book.get("services") or []
        parts = started_job_for_price_book.get("parts") or []
        equipments = started_job_for_price_book.get("equipments") or []
        labors = started_job_for_price_book.get("labors") or []
        materials = started_job_for_price_book.get("materials") or []

        for item in services:
            if not isinstance(item, dict):
                continue
            price_book_items_all.append(
                {
                    "name": _first_non_empty([item.get("name"), item.get("type"), item.get("service_code"), item.get("description")]),
                    "amount": _first_non_empty([item.get("standard_price"), item.get("price"), item.get("cost")]),
                    "view_url": _resolve_price_book_view_url("service", item),
                }
            )

        for item in parts:
            if not isinstance(item, dict):
                continue
            price_book_items_all.append(
                {
                    "name": _first_non_empty([item.get("part_name"), item.get("name"), item.get("part_number"), item.get("description")]),
                    "amount": _first_non_empty([item.get("price"), item.get("cost"), item.get("part_price"), item.get("part_cost")]),
                    "view_url": _resolve_price_book_view_url("part", item),
                }
            )

        for item in equipments:
            if not isinstance(item, dict):
                continue
            price_book_items_all.append(
                {
                    "name": _first_non_empty([item.get("equipment_name"), item.get("name"), item.get("description")]),
                    "amount": _first_non_empty([item.get("price"), item.get("cost"), item.get("equipment_price"), item.get("equipment_cost")]),
                    "view_url": _resolve_price_book_view_url("equipment", item),
                }
            )

        for item in labors:
            if not isinstance(item, dict):
                continue
            price_book_items_all.append(
                {
                    "name": _first_non_empty([item.get("labor_name"), item.get("description"), item.get("name")]),
                    "amount": _first_non_empty([item.get("price"), item.get("cost"), item.get("labor_price"), item.get("labor_cost")]),
                    "view_url": _resolve_price_book_view_url("labor", item),
                }
            )

        for item in materials:
            if not isinstance(item, dict):
                continue
            price_book_items_all.append(
                {
                    "name": _first_non_empty([item.get("material_name"), item.get("name"), item.get("description")]),
                    "amount": _first_non_empty([item.get("price"), item.get("cost"), item.get("material_price"), item.get("material_cost")]),
                    "view_url": _resolve_price_book_view_url("material", item),
                }
            )

    price_book_page_raw = request.args.get("price_book_page", "1")
    try:
        price_book_page = max(1, int(price_book_page_raw))
    except ValueError:
        price_book_page = 1

    price_book_per_page = 5
    price_book_total_pages = (len(price_book_items_all) + price_book_per_page - 1) // price_book_per_page
    if price_book_total_pages == 0:
        price_book_page = 1
        price_book_items = []
    else:
        if price_book_page > price_book_total_pages:
            price_book_page = price_book_total_pages
        price_book_start = (price_book_page - 1) * price_book_per_page
        price_book_end = price_book_start + price_book_per_page
        price_book_items = price_book_items_all[price_book_start:price_book_end]

    # Detect if the current employee already has an active (En Route / Started) job.
    # Used to disable En Route / Start buttons on other job cards.
    _active_statuses = {"en route", "started"}
    current_employee_active_job_id = None
    for _j in jobs_list:
        _j_status = str(_j.get("status") or "").strip().lower()
        if _j_status not in _active_statuses:
            continue
        _j_assigned = " ".join(str(_j.get("assigned_employee") or "").lower().split())
        if _j_assigned and _j_assigned == normalized_current_employee_name:
            current_employee_active_job_id = str(_j.get("_id") or "")
            break

    return render_template(
        "index.html",
        is_logged_in=True,
        home_view_mode=home_view_mode,
        jobs=jobs_list,
        employee_filters=employee_filters,
        pending_jobs=pending_jobs,
        pending_page=pending_page,
        pending_total_pages=pending_total_pages,
        hvac_systems=hvac_systems_payload,
        hvac_page=hvac_page,
        hvac_total_pages=hvac_total_pages,
        current_property=current_property,
        active_job=active_job,
        started_job_for_notes=started_job_for_notes,
        notes=notes,
        notes_page=notes_page,
        notes_total_pages=notes_total_pages,
        customer_notes=customer_notes,
        customer_notes_page=customer_notes_page,
        customer_notes_total_pages=customer_notes_total_pages,
        active_customer_for_notes=active_customer_for_notes,
        started_job_for_price_book=started_job_for_price_book,
        price_book_items=price_book_items,
        price_book_page=price_book_page,
        price_book_total_pages=price_book_total_pages,
        current_employee_active_job_id=current_employee_active_job_id,
        activity_events=activity_events,
        business_center_address=business_center_address,
        google_maps_api_key=(os.getenv("GOOGLE_MAPS_API_KEY") or "").strip(),
    )


@app.route("/error")
def error_page():
    error_type = request.args.get("error", "unknown")
    error_messages = {
        "no_business": "No business onboarded for logged in employee",
        "unknown": "An error occurred",
    }
    error_message = error_messages.get(error_type, error_messages["unknown"])
    return render_template("error.html", error_message=error_message)


@app.route("/privacy-policy")
def privacy_policy():
    return render_template("legal/privacy_policy.html")


@app.route("/terms-and-conditions")
def terms_and_conditions():
    return render_template("legal/terms_and_conditions.html")


@app.route("/invoices/<filename>")
def download_invoice(filename):
    """Serve invoice, estimate, and diagnostic report PDFs from the invoices directory."""
    invoices_dir = os.path.join(os.path.dirname(__file__), "invoices")
    filepath = os.path.join(invoices_dir, filename)

    if not (os.path.exists(filepath) and os.path.abspath(filepath).startswith(os.path.abspath(invoices_dir))):
        return "Invoice not found", 404

    db = ensure_connection_or_500()
    invoice_path_suffix = f"/invoices/{filename}"
    
    # Try to find in jobs (invoices)
    matching_job = db.jobs.find_one(
        {
            "invoices.file_path": {
                "$regex": re.escape(invoice_path_suffix) + r"$",
            }
        },
        {"invoices": 1, "business_id": 1},
    )
    
    matching_estimate = None
    if not matching_job:
        # Try to find in estimates
        matching_estimate = db.estimates.find_one(
            {
                "$or": [
                    {"latest_file_path": {"$regex": re.escape(invoice_path_suffix) + r"$"}},
                    {"file_path": {"$elemMatch": {"file_path": {"$regex": re.escape(invoice_path_suffix) + r"$"}}}},
                ]
            },
            {"latest_file_path": 1, "file_path": 1, "business_id": 1},
        )
        if not matching_estimate:
            # Try to find in HVAC diagnostic reports.
            matching_report = db.hvacDiagnostics.find_one(
                {
                    "reports.file_path": {
                        "$regex": re.escape(f"/invoices/{filename}") + r"$",
                    }
                },
                {"business_id": 1, "customer_id": 1, "hvac_system_id": 1, "reports": 1},
            )
            if not matching_report:
                return "Invoice not found", 404

            employee_id = session.get("employee_id")
            if not employee_id or not ObjectId.is_valid(employee_id):
                return redirect(url_for("auth.login"))

            employee = db.employees.find_one({"_id": ObjectId(employee_id)}, {"business": 1}) or {}
            employee_business_id = str(employee.get("business") or "").strip()
            report_business_id = str(matching_report.get("business_id") or "").strip()

            if employee_business_id and report_business_id and employee_business_id != report_business_id:
                return "Forbidden", 403

            return send_file(filepath, mimetype="application/pdf", as_attachment=False)
    
    if matching_job:
        # Authorization for invoice (from jobs collection)
        matching_invoice = None
        for invoice in matching_job.get("invoices") or []:
            if not isinstance(invoice, dict):
                continue
            file_path = str(invoice.get("file_path") or "").strip()
            if file_path.endswith(invoice_path_suffix):
                matching_invoice = invoice
                break

        if not matching_invoice:
            return "Invoice not found", 404

        employee_id = session.get("employee_id")
        if employee_id and ObjectId.is_valid(employee_id):
            employee = db.employees.find_one({"_id": ObjectId(employee_id)}, {"business": 1}) or {}
            employee_business = employee.get("business")
            employee_business_id = str(employee_business or "").strip()
            job_business_id = str(matching_job.get("business_id") or "").strip()
            if employee_business_id and job_business_id and employee_business_id == job_business_id:
                return send_file(filepath, mimetype="application/pdf", as_attachment=False)
            return "Forbidden", 403

        token_value = str(request.args.get("token") or "").strip()
        if not token_value:
            return redirect(url_for("auth.login"))

        from blueprints.jobs import _verify_invoice_access_token

        if not _verify_invoice_access_token(matching_invoice, token_value):
            return "Forbidden", 403

        return send_file(filepath, mimetype="application/pdf", as_attachment=False)
    
    else:
        # Authorization for estimate (from estimates collection)
        employee_id = session.get("employee_id")
        if employee_id and ObjectId.is_valid(employee_id):
            employee = db.employees.find_one({"_id": ObjectId(employee_id)}, {"business": 1}) or {}
            employee_business = employee.get("business")
            employee_business_id = str(employee_business or "").strip()
            estimate_business_id = str(matching_estimate.get("business_id") or "").strip()
            if employee_business_id and estimate_business_id and employee_business_id == estimate_business_id:
                return send_file(filepath, mimetype="application/pdf", as_attachment=False)
            return "Forbidden", 403

        token_value = str(request.args.get("token") or "").strip()
        if not token_value:
            return redirect(url_for("auth.login"))

        from blueprints.jobs import _verify_estimate_access_token

        if not _verify_estimate_access_token(matching_estimate, token_value):
            return "Forbidden", 403

        return send_file(filepath, mimetype="application/pdf", as_attachment=False)


@app.route("/payments/stripe/webhook", methods=["POST"])
@csrf.exempt
def stripe_webhook():
    webhook_secret = str(os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
    stripe_secret_key = str(os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not webhook_secret or not stripe_secret_key:
        return "Stripe webhook is not configured", 500

    stripe.api_key = stripe_secret_key
    payload = request.get_data(as_text=True)
    signature = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, signature, webhook_secret)
    except ValueError as exc:
        app.logger.warning("Stripe webhook payload parse failed: %s", exc)
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError as exc:
        app.logger.warning("Stripe webhook signature verification failed: %s", exc)
        return "Invalid signature", 400

    if event.get("type") in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        from blueprints.jobs import process_stripe_checkout_completed

        db = ensure_connection_or_500()
        process_stripe_checkout_completed(db, event.get("data", {}).get("object", {}))

    return "", 200


def _invoice_reminder_scheduler_enabled():
    if not scheduler_enabled_flag():
        return False

    if os.getenv("PYTEST_CURRENT_TEST"):
        return False

    if app.debug:
        return os.getenv("WERKZEUG_RUN_MAIN") == "true"

    return True


def _invoice_reminder_scheduler_interval_minutes():
    return scheduler_interval_minutes()


def _run_invoice_reminder_scheduler_tick():
    try:
        from blueprints.jobs import process_due_invoice_reminders

        with app.app_context():
            db = ensure_connection_or_500()
            processed_count = process_due_invoice_reminders(db=db, batch_size=200)
            if processed_count:
                app.logger.info("Invoice reminder scheduler processed %s due reminder(s)", processed_count)
    except Exception as exc:
        app.logger.error("Invoice reminder scheduler tick failed: %s", exc)


def _start_invoice_reminder_scheduler():
    global _invoice_reminder_scheduler
    if _invoice_reminder_scheduler is not None:
        return

    if not _invoice_reminder_scheduler_enabled():
        return

    scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
    scheduler.add_job(
        _run_invoice_reminder_scheduler_tick,
        trigger="interval",
        minutes=_invoice_reminder_scheduler_interval_minutes(),
        id="invoice_reminder_hourly",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _invoice_reminder_scheduler = scheduler
    app.logger.info("Invoice reminder scheduler started")

    def _shutdown_scheduler():
        if _invoice_reminder_scheduler is not None:
            _invoice_reminder_scheduler.shutdown(wait=False)

    atexit.register(_shutdown_scheduler)


_start_invoice_reminder_scheduler()


if __name__ == "__main__":
    app.run(debug=True)
