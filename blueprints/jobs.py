import calendar
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import os
import secrets

from bson import ObjectId
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for
from flask_mail import Message
import stripe

from invoice_generator import generate_estimate, generate_invoice
from mongo import build_reference_filter, coerce_object_id, ensure_connection_or_500, object_id_or_404, reference_value, serialize_doc
from utils.catalog import (
    build_discount_catalog,
    build_job_discounts_from_form,
    build_job_equipments_from_form,
    build_job_labors_from_form,
    build_job_materials_from_form,
    build_job_parts_from_form,
    build_job_services_from_form,
    build_equipment_catalog,
    build_labor_catalog,
    build_material_catalog,
    build_part_catalog,
    build_service_catalog,
)
from utils.currency import currency_to_float, normalize_currency
from utils.csv_export import build_csv_export_response
from utils.formatters import format_date

bp = Blueprint("jobs", __name__)

RECURRING_FREQUENCY_OPTIONS = (
    ("weekly", "Weekly"),
    ("biweekly", "Every 2 Weeks"),
    ("monthly", "Monthly"),
    ("quarterly", "Quarterly"),
    ("semiannual", "Semiannual"),
    ("annual", "Annual"),
)

RECURRING_FREQUENCY_LABELS = dict(RECURRING_FREQUENCY_OPTIONS)

RECURRING_END_TYPE_OPTIONS = (
    ("never", "Never"),
    ("on_date", "On Date"),
    ("after_occurrences", "After Number of Visits"),
)

RECURRING_END_TYPE_LABELS = dict(RECURRING_END_TYPE_OPTIONS)


def _parse_mmddyyyy_date(date_text):
    raw_date = str(date_text or "").strip()
    if not raw_date:
        return None
    try:
        return datetime.strptime(raw_date, "%m/%d/%Y")
    except ValueError:
        return None


def _format_mmddyyyy_date(value):
    if not value:
        return ""
    return value.strftime("%m/%d/%Y")


def _mmddyyyy_to_iso_date(value):
    parsed = _parse_mmddyyyy_date(value)
    if not parsed:
        return ""
    return parsed.strftime("%Y-%m-%d")


def _iso_datetime_to_utc_parts(value):
    raw_value = str(value or "").strip()
    if not raw_value:
        return "", "", ""
    try:
        normalized = raw_value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return "", "", ""

    return (
        parsed.strftime("%Y-%m-%d"),
        parsed.strftime("%H:%M:%S"),
        parsed.strftime("%m/%d/%Y"),
    )


def _normalize_payment_due_days(value, fallback=30):
    try:
        normalized = int(str(value or "").strip())
    except (TypeError, ValueError):
        normalized = int(fallback)
    return max(1, normalized)


def _resolve_default_payment_due_days(db, fallback=30):
    business_id = resolve_current_business_id(db)
    if not business_id:
        return _normalize_payment_due_days(fallback, fallback)

    business_doc = db.businesses.find_one({"_id": business_id}, {"default_payment_due_days": 1})
    if not business_doc:
        return _normalize_payment_due_days(fallback, fallback)

    return _normalize_payment_due_days(business_doc.get("default_payment_due_days"), fallback)


def _add_months(value, months):
    if not value:
        return None

    month_index = (value.month - 1) + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _advance_recurring_date(value, frequency):
    if not value:
        return None

    if frequency == "weekly":
        return value + timedelta(days=7)
    if frequency == "biweekly":
        return value + timedelta(days=14)
    if frequency == "monthly":
        return _add_months(value, 1)
    if frequency == "quarterly":
        return _add_months(value, 3)
    if frequency == "semiannual":
        return _add_months(value, 6)
    if frequency == "annual":
        return _add_months(value, 12)
    return None


def _build_recurrence_summary(frequency):
    label = RECURRING_FREQUENCY_LABELS.get(str(frequency or "").strip(), "")
    return f"Recurring {label}".strip() if label else ""


def _parse_note_datetime(value):
    raw_value = str(value or "").strip()
    if not raw_value:
        return datetime.min
    try:
        return datetime.strptime(raw_value, "%m/%d/%Y %H:%M:%S")
    except ValueError:
        return datetime.min


def _build_internal_notes_for_view(db, job):
    raw_notes = list((job or {}).get("internal_notes") or [])
    normalized_notes = []
    employee_object_ids = []

    for note in raw_notes:
        if not isinstance(note, dict):
            continue

        employee_id = str(note.get("employee_id") or "").strip()
        if ObjectId.is_valid(employee_id):
            employee_object_ids.append(ObjectId(employee_id))

        normalized_notes.append(
            {
                "note_id": str(note.get("note_id") or "").strip(),
                "text": str(note.get("text") or "").strip(),
                "date_written": str(note.get("date_written") or "").strip(),
                "employee_id": employee_id,
            }
        )

    employees_by_id = {}
    if employee_object_ids:
        for employee in db.employees.find({"_id": {"$in": employee_object_ids}}, {"first_name": 1, "last_name": 1}):
            employee_id = str(employee.get("_id") or "").strip()
            full_name = f"{str(employee.get('first_name') or '').strip()} {str(employee.get('last_name') or '').strip()}".strip()
            employees_by_id[employee_id] = full_name or "Unknown Employee"

    normalized_notes.sort(key=lambda note: _parse_note_datetime(note.get("date_written")), reverse=True)

    for note in normalized_notes:
        employee_id = note.get("employee_id") or ""
        note["employee_name"] = employees_by_id.get(employee_id, "Unknown Employee")
        note["employee_profile_id"] = employee_id if ObjectId.is_valid(employee_id) else ""

    return normalized_notes


def _build_recurrence_form_state(job=None, series=None):
    if job and str(job.get("job_kind") or "").strip() == "recurring_occurrence":
        source = series or {}
        return {
            "schedule_type": "recurring",
            "frequency": str(source.get("frequency") or "").strip(),
            "end_type": str(source.get("end_type") or "never").strip() or "never",
            "end_date": str(source.get("end_date") or "").strip(),
            "max_occurrences": str(source.get("max_occurrences") or "").strip(),
        }

    return {
        "schedule_type": "one_time",
        "frequency": "",
        "end_type": "never",
        "end_date": "",
        "max_occurrences": "",
    }


def _parse_recurrence_request(request_obj, scheduled_date, scheduled_time, existing_series=None, lock_to_recurring=False):
    requested_type = str(request_obj.form.get("job_schedule_type") or "one_time").strip() or "one_time"
    schedule_type = "recurring" if lock_to_recurring else requested_type
    frequency = str(request_obj.form.get("recurring_frequency") or "").strip()
    end_type = str(request_obj.form.get("recurring_end_type") or "never").strip() or "never"
    end_date = format_date(request_obj.form.get("recurring_end_date", "")) if end_type == "on_date" else ""

    max_occurrences = None
    raw_max_occurrences = str(request_obj.form.get("recurring_end_after", "")).strip()
    if end_type == "after_occurrences" and raw_max_occurrences.isdigit():
        max_occurrences = max(int(raw_max_occurrences), 1)

    if schedule_type != "recurring":
        return {
            "schedule_type": "one_time",
            "is_recurring": False,
            "frequency": "",
            "end_type": "never",
            "end_date": "",
            "max_occurrences": None,
            "summary": "",
        }

    if not (scheduled_date and scheduled_time and frequency in RECURRING_FREQUENCY_LABELS):
        if existing_series:
            return {
                "schedule_type": "recurring",
                "is_recurring": True,
                "frequency": str(existing_series.get("frequency") or "").strip(),
                "end_type": str(existing_series.get("end_type") or "never").strip() or "never",
                "end_date": str(existing_series.get("end_date") or "").strip(),
                "max_occurrences": existing_series.get("max_occurrences"),
                "summary": _build_recurrence_summary(existing_series.get("frequency")),
            }
        return {
            "schedule_type": "one_time",
            "is_recurring": False,
            "frequency": "",
            "end_type": "never",
            "end_date": "",
            "max_occurrences": None,
            "summary": "",
        }

    return {
        "schedule_type": "recurring",
        "is_recurring": True,
        "frequency": frequency,
        "end_type": end_type,
        "end_date": end_date,
        "max_occurrences": max_occurrences,
        "summary": _build_recurrence_summary(frequency),
    }


def _series_allows_occurrence(series_doc, occurrence_index, scheduled_date):
    if not series_doc or str(series_doc.get("status") or "").strip() != "Active":
        return False

    end_type = str(series_doc.get("end_type") or "never").strip() or "never"
    if end_type == "after_occurrences":
        max_occurrences = series_doc.get("max_occurrences")
        if isinstance(max_occurrences, int) and max_occurrences > 0 and occurrence_index > max_occurrences:
            return False

    if end_type == "on_date":
        end_date = _parse_mmddyyyy_date(series_doc.get("end_date"))
        scheduled_dt = _parse_mmddyyyy_date(scheduled_date)
        if end_date and scheduled_dt and scheduled_dt > end_date:
            return False

    return True


def _build_recurring_series_document(customer, business_id, selected_property, selected_property_id, primary_service, services, parts, labors, materials, equipments, discounts, total, assigned_employee, recurring_data, scheduled_date, scheduled_time, payment_due_days_offset, request_obj):
    series_anchor_date = scheduled_date
    anchor_date_dt = _parse_mmddyyyy_date(series_anchor_date)
    next_occurrence_date = _advance_recurring_date(anchor_date_dt, recurring_data.get("frequency"))
    next_occurrence_text = _format_mmddyyyy_date(next_occurrence_date)

    try:
        recurring_due_offset = int(str(payment_due_days_offset or "").strip())
    except (TypeError, ValueError):
        recurring_due_offset = 30
    recurring_due_offset = max(0, recurring_due_offset)

    series_doc = {
        "customer_id": reference_value(customer.get("_id")),
        "customer_name": f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip(),
        "company": customer.get("company", ""),
        "property_id": selected_property_id if selected_property else "",
        "property_name": (selected_property or {}).get("property_name") or "",
        "job_type": primary_service,
        "services": services,
        "parts": parts,
        "labors": labors,
        "materials": materials,
        "equipments": equipments,
        "discounts": discounts,
        "status": "Active",
        "frequency": recurring_data.get("frequency"),
        "anchor_date": series_anchor_date,
        "anchor_time": scheduled_time,
        "end_type": recurring_data.get("end_type") or "never",
        "end_date": recurring_data.get("end_date") or "",
        "max_occurrences": recurring_data.get("max_occurrences"),
        "next_occurrence_date": next_occurrence_text,
        "last_generated_occurrence_index": 0,
        "address_line_1": request_obj.form.get("job_address_line_1", "").strip(),
        "address_line_2": request_obj.form.get("job_address_line_2", "").strip(),
        "city": request_obj.form.get("job_city", "").strip(),
        "state": request_obj.form.get("job_state", "").strip().upper(),
        "zip_code": request_obj.form.get("job_zip_code", "").strip(),
        "assigned_employee": assigned_employee,
        "total_amount": float(total or 0.0),
        "invoice_notes": request_obj.form.get("invoice_notes", "").strip(),
        "payment_due_days_offset": recurring_due_offset,
        "business_id": business_id,
        "created_at": datetime.utcnow(),
    }

    if next_occurrence_text and not _series_allows_occurrence(series_doc, 2, next_occurrence_text):
        series_doc["next_occurrence_date"] = ""

    return series_doc


def _create_occurrence_from_series(db, series_doc, scheduled_date, occurrence_index):
    if not series_doc:
        return None

    series_id = series_doc.get("_id")
    if series_id and db.jobs.find_one({"series_id": series_id, "occurrence_index": occurrence_index}):
        return None

    if not _series_allows_occurrence(series_doc, occurrence_index, scheduled_date):
        if series_id:
            db.recurring_job_series.update_one(
                {"_id": series_id},
                {"$set": {"next_occurrence_date": "", "last_generated_occurrence_index": occurrence_index - 1}},
            )
        return None

    services = list(series_doc.get("services") or [])
    parts = list(series_doc.get("parts") or [])
    labors = list(series_doc.get("labors") or [])
    materials = list(series_doc.get("materials") or [])
    equipments = list(series_doc.get("equipments") or [])
    discounts = list(series_doc.get("discounts") or [])
    scheduled_time = str(series_doc.get("anchor_time") or "").strip()
    try:
        payment_due_days_offset = int(str(series_doc.get("payment_due_days_offset") or "").strip())
    except (TypeError, ValueError):
        payment_due_days_offset = 30
    payment_due_days_offset = max(0, payment_due_days_offset)
    payment_due_days = payment_due_days_offset
    occurrence_doc = {
        "customer_id": series_doc.get("customer_id"),
        "customer_name": str(series_doc.get("customer_name") or "").strip(),
        "company": str(series_doc.get("company") or "").strip(),
        "property_id": series_doc.get("property_id") or "",
        "property_name": str(series_doc.get("property_name") or "").strip(),
        "job_type": str(series_doc.get("job_type") or "No services added.").strip(),
        "services": services,
        "parts": parts,
        "labors": labors,
        "materials": materials,
        "equipments": equipments,
        "discounts": discounts,
        "status": resolve_job_status(scheduled_date, scheduled_time, services, parts, labors, materials, equipments, discounts),
        "scheduled_date": scheduled_date,
        "scheduled_time": scheduled_time,
        "dateScheduled": datetime.now().strftime("%m/%d/%Y") if (scheduled_date and scheduled_time) else "",
        "address_line_1": str(series_doc.get("address_line_1") or "").strip(),
        "address_line_2": str(series_doc.get("address_line_2") or "").strip(),
        "city": str(series_doc.get("city") or "").strip(),
        "state": str(series_doc.get("state") or "").strip().upper(),
        "zip_code": str(series_doc.get("zip_code") or "").strip(),
        "assigned_employee": str(series_doc.get("assigned_employee") or "").strip(),
        "total_amount": float(series_doc.get("total_amount") or 0.0),
        "invoice_notes": str(series_doc.get("invoice_notes") or "").strip(),
        "payment_due_days": payment_due_days,
        "internal_notes": [],
        "date_created": datetime.now().strftime("%m/%d/%Y"),
        "created_at": datetime.utcnow(),
        "invoices": [],
        "business_id": series_doc.get("business_id"),
        "job_kind": "recurring_occurrence",
        "series_id": series_id,
        "occurrence_index": occurrence_index,
        "recurrence_summary": _build_recurrence_summary(series_doc.get("frequency")),
    }
    inserted = db.jobs.insert_one(occurrence_doc)

    current_occurrence_date = _parse_mmddyyyy_date(scheduled_date)
    next_occurrence_date = _advance_recurring_date(current_occurrence_date, series_doc.get("frequency"))
    next_occurrence_text = _format_mmddyyyy_date(next_occurrence_date)
    if next_occurrence_text and not _series_allows_occurrence(series_doc, occurrence_index + 1, next_occurrence_text):
        next_occurrence_text = ""

    if series_id:
        db.recurring_job_series.update_one(
            {"_id": series_id},
            {
                "$set": {
                    "last_generated_occurrence_index": occurrence_index,
                    "next_occurrence_date": next_occurrence_text,
                }
            },
        )

    return str(inserted.inserted_id)


def _combine_scheduled_datetime(date_text, time_text):
    raw_date = str(date_text or "").strip()
    raw_time = str(time_text or "").strip()
    if not raw_date or not raw_time:
        return None

    for date_fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(f"{raw_date} {raw_time}", f"{date_fmt} %H:%M")
        except ValueError:
            continue
    return None


def resolve_current_business_logo_path(db):
    employee_id = session.get("employee_id")
    if not employee_id or not ObjectId.is_valid(employee_id):
        return ""

    employee = db.employees.find_one({"_id": ObjectId(employee_id)}, {"business": 1})
    if not employee:
        return ""

    business_ref = employee.get("business")
    business_oid = None
    if isinstance(business_ref, ObjectId):
        business_oid = business_ref
    elif isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
        business_oid = ObjectId(business_ref)

    if not business_oid:
        return ""

    business = db.businesses.find_one({"_id": business_oid}, {"custom_logo": 1})
    custom_logo = os.path.basename(str((business or {}).get("custom_logo") or "").strip())
    if not custom_logo:
        return ""

    logo_path = os.path.join(current_app.root_path, "static", "uploads", "logos", custom_logo)
    return logo_path if os.path.exists(logo_path) else ""


def resolve_job_status(scheduled_date, scheduled_time, services, parts, labors, materials, equipments, discounts, existing_status=""):
    """Derive job status from scheduling and line items while preserving terminal states."""
    normalized_existing = str(existing_status or "").strip().lower()
    if normalized_existing in {"started", "completed", "paid"}:
        return str(existing_status)

    has_schedule = bool(str(scheduled_date).strip()) and bool(str(scheduled_time).strip())
    has_line_items = bool(services or parts or labors or materials or equipments or discounts)

    if has_schedule and has_line_items:
        return "Scheduled"
    if has_schedule:
        return "Scheduled"
    if has_line_items:
        return "Pending"
    return "Pending"


def resolve_estimate_file_path(estimate):
    latest_path = str((estimate or {}).get("latest_file_path") or "").strip()
    if latest_path:
        return latest_path

    file_path = (estimate or {}).get("file_path")
    if isinstance(file_path, str):
        return file_path
    if isinstance(file_path, list):
        for item in reversed(file_path):
            if isinstance(item, dict):
                candidate = str(item.get("file_path") or "").strip()
                if candidate:
                    return candidate
    return ""


def normalize_estimate_file_history(file_path_value):
    if isinstance(file_path_value, list):
        normalized = []
        for item in file_path_value:
            if isinstance(item, dict):
                candidate = str(item.get("file_path") or "").strip()
                if candidate:
                    normalized.append({"file_path": candidate})
            elif isinstance(item, str) and item.strip():
                normalized.append({"file_path": item.strip()})
        return normalized
    if isinstance(file_path_value, str) and file_path_value.strip():
        return [{"file_path": file_path_value.strip()}]
    return []


def serialize_estimate_for_pdf(estimate):
    serialized = dict(estimate or {})
    estimated_by = str(serialized.get("estimated_by_employee") or "").strip()
    serialized["assigned_employee"] = estimated_by
    serialized["scheduled_date"] = str(serialized.get("proposed_job_date") or "").strip()
    serialized["scheduled_time"] = str(serialized.get("proposed_job_time") or "").strip()
    estimate_notes = str(serialized.get("estimate_notes") or "").strip()
    serialized["notes"] = [{"text": estimate_notes}] if estimate_notes else []
    return serialized


def _build_estimate_recurrence_form_state(estimate=None):
    source = estimate or {}
    schedule_type = "recurring" if str(source.get("job_schedule_type") or "").strip() == "recurring" else "one_time"
    if schedule_type != "recurring":
        return {
            "schedule_type": "one_time",
            "frequency": "",
            "end_type": "never",
            "end_date": "",
            "max_occurrences": "",
        }

    max_occurrences = source.get("recurring_end_after")
    if max_occurrences in (None, ""):
        max_occurrences = source.get("max_occurrences")

    return {
        "schedule_type": "recurring",
        "frequency": str(source.get("recurring_frequency") or "").strip(),
        "end_type": str(source.get("recurring_end_type") or "never").strip() or "never",
        "end_date": str(source.get("recurring_end_date") or "").strip(),
        "max_occurrences": str(max_occurrences or "").strip(),
    }


def _clone_line_item_list(value):
    cloned = []
    for item in (value or []):
        if isinstance(item, dict):
            cloned.append(dict(item))
    return cloned


def _create_job_from_accepted_estimate(db, estimate_id):
    estimate = db.estimates.find_one({"_id": object_id_or_404(estimate_id)})
    if not estimate:
        return ""

    existing_job_id = str(estimate.get("created_job_id") or "").strip()
    if existing_job_id and ObjectId.is_valid(existing_job_id):
        existing_job = db.jobs.find_one({"_id": ObjectId(existing_job_id)}, {"_id": 1})
        if existing_job:
            return existing_job_id

    schedule_type = "recurring" if str(estimate.get("job_schedule_type") or "").strip() == "recurring" else "one_time"
    recurring_frequency = str(estimate.get("recurring_frequency") or "").strip() if schedule_type == "recurring" else ""
    recurring_end_type = str(estimate.get("recurring_end_type") or "never").strip() if schedule_type == "recurring" else "never"
    recurring_end_date = str(estimate.get("recurring_end_date") or "").strip() if schedule_type == "recurring" else ""
    recurring_end_after = estimate.get("recurring_end_after") if schedule_type == "recurring" else None
    recurrence_summary = str(estimate.get("recurrence_summary") or "").strip() if schedule_type == "recurring" else ""
    if schedule_type == "recurring" and not recurrence_summary:
        recurrence_summary = _build_recurrence_summary(recurring_frequency)

    services = _clone_line_item_list(estimate.get("services"))
    parts = _clone_line_item_list(estimate.get("parts"))
    labors = _clone_line_item_list(estimate.get("labors"))
    materials = _clone_line_item_list(estimate.get("materials"))
    equipments = _clone_line_item_list(estimate.get("equipments"))
    discounts = _clone_line_item_list(estimate.get("discounts"))

    proposed_date = str(estimate.get("proposed_job_date") or "").strip()
    proposed_time = str(estimate.get("proposed_job_time") or "").strip()
    date_scheduled = proposed_date if (proposed_date and proposed_time) else ""

    customer_name = str(estimate.get("customer_name") or "").strip()
    customer_company = str(estimate.get("company") or "").strip()
    customer_id_ref = estimate.get("customer_id")
    customer_doc = None
    if customer_id_ref:
        customer_doc = db.customers.find_one(build_reference_filter("_id", customer_id_ref))

    if customer_doc and not customer_name:
        customer_name = f"{customer_doc.get('first_name', '')} {customer_doc.get('last_name', '')}".strip()
    if customer_doc and not customer_company:
        customer_company = str(customer_doc.get("company") or "").strip()
    if not customer_name:
        customer_name = "Unknown Customer"

    primary_service = services[0].get("type") if services else "No services added."
    payment_due_days = _resolve_default_payment_due_days(db)
    business_id = estimate.get("business_id") or resolve_current_business_id(db)

    new_job = {
        "customer_id": customer_id_ref,
        "customer_name": customer_name,
        "company": customer_company,
        "property_id": str(estimate.get("property_id") or "").strip(),
        "property_name": str(estimate.get("property_name") or "").strip(),
        "job_type": primary_service,
        "services": services,
        "parts": parts,
        "labors": labors,
        "materials": materials,
        "equipments": equipments,
        "discounts": discounts,
        "status": "Scheduled",
        "scheduled_date": proposed_date,
        "scheduled_time": proposed_time,
        "dateScheduled": date_scheduled,
        "address_line_1": str(estimate.get("address_line_1") or "").strip(),
        "address_line_2": str(estimate.get("address_line_2") or "").strip(),
        "city": str(estimate.get("city") or "").strip(),
        "state": str(estimate.get("state") or "").strip().upper(),
        "zip_code": str(estimate.get("zip_code") or "").strip(),
        "assigned_employee": str(estimate.get("created_by_employee") or "").strip(),
        "total_amount": float(estimate.get("total_amount") or 0.0),
        "invoice_notes": "",
        "payment_due_days": payment_due_days,
        "internal_notes": [],
        "date_created": datetime.now().strftime("%m/%d/%Y"),
        "created_at": datetime.utcnow(),
        "invoices": [],
        "business_id": business_id,
        "job_kind": "one_time",
        "series_id": None,
        "occurrence_index": None,
        "recurrence_summary": recurrence_summary,
        "job_schedule_type": schedule_type,
        "recurring_frequency": recurring_frequency,
        "recurring_end_type": recurring_end_type,
        "recurring_end_date": recurring_end_date,
        "recurring_end_after": recurring_end_after,
        "source_estimate_id": ObjectId(estimate_id),
    }

    inserted = db.jobs.insert_one(new_job)
    created_job_id = str(inserted.inserted_id)
    db.estimates.update_one(
        {"_id": ObjectId(estimate_id)},
        {
            "$set": {
                "created_job_id": created_job_id,
                "job_created_from_estimate_at": datetime.utcnow(),
            }
        },
    )

    return created_job_id


def remove_estimate_pdf_file(file_url):
    filename = str(file_url or "").strip().split("/")[-1]
    if not filename:
        return ""

    base_dir = os.path.dirname(os.path.dirname(__file__))
    invoices_dir = os.path.join(base_dir, "invoices")
    filepath = os.path.abspath(os.path.join(invoices_dir, filename))
    invoices_dir_abs = os.path.abspath(invoices_dir)

    if not filepath.startswith(invoices_dir_abs):
        return ""

    if os.path.exists(filepath):
        try:
            os.remove(filepath)
        except OSError as exc:
            current_app.logger.warning("Failed to remove old estimate PDF: path=%s error=%s", filepath, exc)

    return filepath


def estimate_pdf_absolute_path_from_url(file_url):
    filename = str(file_url or "").strip().split("/")[-1]
    if not filename:
        return ""

    base_dir = os.path.dirname(os.path.dirname(__file__))
    invoices_dir = os.path.abspath(os.path.join(base_dir, "invoices"))
    filepath = os.path.abspath(os.path.join(invoices_dir, filename))

    if not filepath.startswith(invoices_dir):
        return ""
    return filepath


def build_employee_options(db):
    employee_docs = [
        serialize_doc(employee)
        for employee in db.employees.find().sort([("last_name", 1), ("first_name", 1)])
    ]

    employee_options = []
    for employee in employee_docs:
        first_name = str(employee.get("first_name", "")).strip()
        last_name = str(employee.get("last_name", "")).strip()
        full_name = f"{first_name} {last_name}".strip()
        if full_name:
            employee_options.append({
                "id": employee.get("_id", ""),
                "name": full_name,
            })

    return employee_options


def resolve_current_business_id(db):
    employee_id = session.get("employee_id")
    if not employee_id or not ObjectId.is_valid(employee_id):
        return None

    employee = db.employees.find_one({"_id": ObjectId(employee_id)}, {"business": 1})
    business_ref = (employee or {}).get("business")
    if isinstance(business_ref, ObjectId):
        return business_ref
    if isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
        return ObjectId(business_ref)
    return None


def _is_authenticated_employee():
    employee_id = session.get("employee_id")
    return bool(employee_id and ObjectId.is_valid(employee_id))


def _extract_client_ip():
    forwarded_for = str(request.headers.get("X-Forwarded-For") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return str(request.remote_addr or "").strip()


def _build_estimate_view_url(estimate_id, access_token=None, external=False):
    token_value = str(access_token or "").strip()
    if token_value:
        return url_for("jobs.view_estimate", estimateId=estimate_id, token=token_value, _external=external)
    return url_for("jobs.view_estimate", estimateId=estimate_id, _external=external)


def _build_invoice_view_url(job_id, invoice_ref, access_token=None, external=False):
    token_value = str(access_token or "").strip()
    if token_value:
        return url_for("jobs.view_invoice", jobId=job_id, invoiceRef=invoice_ref, token=token_value, _external=external)
    return url_for("jobs.view_invoice", jobId=job_id, invoiceRef=invoice_ref, _external=external)


def _issue_estimate_access_token(db, estimate_id, recipient_email=""):
    token_value = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
    db.estimates.update_one(
        {"_id": ObjectId(estimate_id)},
        {
            "$set": {
                "access_token_hash": token_hash,
                "access_token_created_at": datetime.utcnow(),
                "access_token_recipient": str(recipient_email or "").strip(),
            }
        },
    )
    return token_value


def _verify_estimate_access_token(estimate, provided_token):
    token_value = str(provided_token or "").strip()
    stored_hash = str((estimate or {}).get("access_token_hash") or "").strip()
    if not token_value or not stored_hash:
        return False

    candidate_hash = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
    return hmac.compare_digest(candidate_hash, stored_hash)


def _find_invoice_entry(job_doc, invoice_ref="", file_path=""):
    invoices = list((job_doc or {}).get("invoices") or [])
    normalized_ref = str(invoice_ref or "").strip()
    normalized_file_name = str(file_path or "").strip().split("/")[-1]

    for invoice in invoices:
        if not isinstance(invoice, dict):
            continue
        invoice_id = str(invoice.get("invoice_id") or "").strip()
        invoice_number = str(invoice.get("invoice_number") or "").strip()
        invoice_file = str(invoice.get("file_path") or "").strip()
        invoice_file_name = invoice_file.split("/")[-1] if invoice_file else ""

        if normalized_ref and (invoice_id == normalized_ref or invoice_number == normalized_ref):
            return invoice
        if normalized_file_name and invoice_file_name == normalized_file_name:
            return invoice

    return None


def _issue_invoice_access_token(db, job_id, invoice_ref, recipient_email=""):
    job = db.jobs.find_one({"_id": ObjectId(job_id)})
    if not job:
        return ""

    job_doc = serialize_doc(job)
    invoice_entry = _find_invoice_entry(job_doc, invoice_ref=invoice_ref)
    if not invoice_entry:
        return ""

    token_value = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
    normalized_invoice_ref = str(invoice_ref or "").strip()

    updated_invoices = []
    for entry in list(job_doc.get("invoices") or []):
        if not isinstance(entry, dict):
            updated_invoices.append(entry)
            continue

        entry_invoice_id = str(entry.get("invoice_id") or "").strip()
        entry_invoice_number = str(entry.get("invoice_number") or "").strip()
        if normalized_invoice_ref in {entry_invoice_id, entry_invoice_number}:
            updated = dict(entry)
            updated["access_token_hash"] = token_hash
            updated["access_token_created_at"] = datetime.utcnow().isoformat()
            updated["access_token_recipient"] = str(recipient_email or "").strip()
            updated_invoices.append(updated)
        else:
            updated_invoices.append(entry)

    db.jobs.update_one(
        {"_id": ObjectId(job_id)},
        {"$set": {"invoices": updated_invoices}},
    )
    return token_value


def _verify_invoice_access_token(invoice_entry, provided_token):
    token_value = str(provided_token or "").strip()
    stored_hash = str((invoice_entry or {}).get("access_token_hash") or "").strip()
    if not token_value or not stored_hash:
        return False

    candidate_hash = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
    return hmac.compare_digest(candidate_hash, stored_hash)


def _resolve_stripe_secret_key():
    return str(os.getenv("STRIPE_SECRET_KEY") or "").strip()


def _resolve_stripe_publishable_key():
    return str(os.getenv("STRIPE_PUBLISHABLE_KEY") or "").strip()


def _resolve_stripe_webhook_secret():
    return str(os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()


def _resolve_platform_fee_percent(default=0.0):
    raw_value = str(os.getenv("STRIPE_PLATFORM_FEE_PERCENT") or "").strip()
    if not raw_value:
        return float(default)
    try:
        parsed = float(raw_value)
    except ValueError:
        parsed = float(default)
    return max(0.0, min(parsed, 100.0))


def _configure_stripe_client():
    secret_key = _resolve_stripe_secret_key()
    if not secret_key:
        return ""
    stripe.api_key = secret_key
    return secret_key


def _stripe_obj_value(obj, key, default=None):
    if obj is None:
        return default
    try:
        value = getattr(obj, key)
        if value is not None:
            return value
    except Exception:
        pass
    try:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return obj[key]
    except Exception:
        return default


def _stripe_obj_dict(obj):
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    try:
        to_dict_recursive = getattr(obj, "to_dict_recursive", None)
        if callable(to_dict_recursive):
            converted = to_dict_recursive()
            if isinstance(converted, dict):
                return converted
    except Exception:
        pass
    try:
        return dict(obj)
    except Exception:
        pass
    try:
        items = getattr(obj, "items", None)
        if callable(items):
            return {k: v for k, v in items()}
    except Exception:
        pass
    return {}


def _build_invoice_payment_label(job_doc, invoice_entry):
    invoice_number = str((invoice_entry or {}).get("invoice_number") or "").strip()
    customer_name = str((job_doc or {}).get("customer_name") or "").strip()
    if invoice_number and customer_name:
        return f"{invoice_number} for {customer_name}"
    if invoice_number:
        return invoice_number
    return "Invoice Payment"


def _build_job_paid_timestamp_text():
    return datetime.now().strftime("%m/%d/%Y %H:%M:%S")


def _resolve_invoice_identifiers_from_session_id(db, stripe_session_id):
    session_id = str(stripe_session_id or "").strip()
    if not session_id:
        return "", ""

    job = db.jobs.find_one(
        {"invoices.stripe_checkout_session_id": session_id},
        {"_id": 1, "invoices": 1},
    )
    if not job:
        return "", ""

    job_id = str(job.get("_id") or "").strip()
    for entry in list(job.get("invoices") or []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("stripe_checkout_session_id") or "").strip() != session_id:
            continue
        invoice_ref = str(entry.get("invoice_id") or entry.get("invoice_number") or "").strip()
        if invoice_ref:
            return job_id, invoice_ref
    return "", ""


def _finalize_invoice_payment(db, job_id, invoice_ref, stripe_session_id="", payment_intent_id="", amount_paid=0.0):
    job = db.jobs.find_one({"_id": ObjectId(job_id)})
    if not job:
        return False

    job_doc = serialize_doc(job)
    invoice_entry = _find_invoice_entry(job_doc, invoice_ref=invoice_ref)
    if not invoice_entry:
        return False

    if str(invoice_entry.get("payment_status") or "").strip().lower() == "paid":
        return True

    paid_at_utc = datetime.utcnow()
    paid_at_text = _build_job_paid_timestamp_text()
    normalized_invoice_ref = str(invoice_ref or "").strip()

    updated_invoices = []
    for entry in list(job_doc.get("invoices") or []):
        if not isinstance(entry, dict):
            updated_invoices.append(entry)
            continue

        entry_invoice_id = str(entry.get("invoice_id") or "").strip()
        entry_invoice_number = str(entry.get("invoice_number") or "").strip()
        updated_entry = dict(entry)
        if normalized_invoice_ref in {entry_invoice_id, entry_invoice_number}:
            updated_entry["payment_status"] = "paid"
            updated_entry["paid_at"] = paid_at_text
            if stripe_session_id:
                updated_entry["stripe_checkout_session_id"] = stripe_session_id
            if payment_intent_id:
                updated_entry["stripe_payment_intent_id"] = payment_intent_id
            if amount_paid:
                updated_entry["amount_paid"] = round(float(amount_paid), 2)
        updated_invoices.append(updated_entry)

    db.jobs.update_one(
        {"_id": ObjectId(job_id)},
        {
            "$set": {
                "status": "Paid",
                "datePaid": paid_at_text,
                "paid_at": paid_at_utc,
                "updated_at": paid_at_utc,
                "invoices": updated_invoices,
            }
        },
    )

    customer_id = job.get("customer_id")
    if customer_id:
        customer_doc = db.customers.find_one(build_reference_filter("_id", customer_id))
        if customer_doc:
            current_balance = float(customer_doc.get("balance_due_amount", currency_to_float(customer_doc.get("balance_due", "$0.00"))))
            payment_value = float(amount_paid or job.get("total_amount") or 0.0)
            updated_balance_amount = max(0.0, round(current_balance - payment_value, 2))
            db.customers.update_one(
                {"_id": customer_doc.get("_id")},
                {
                    "$set": {
                        "balance_due_amount": updated_balance_amount,
                        "balance_due": normalize_currency(str(updated_balance_amount)),
                    }
                },
            )

    return True


def process_stripe_checkout_completed(db, checkout_session):
    stripe_session_id = str(_stripe_obj_value(checkout_session, "id", "") or "").strip()
    metadata_raw = _stripe_obj_value(checkout_session, "metadata", {}) or {}
    metadata = _stripe_obj_dict(metadata_raw)
    job_id = str(metadata.get("job_id") or "").strip()
    invoice_ref = str(metadata.get("invoice_ref") or "").strip()

    if not job_id or not invoice_ref:
        client_reference_id = str(_stripe_obj_value(checkout_session, "client_reference_id", "") or "").strip()
        if ":" in client_reference_id:
            parsed_job_id, parsed_invoice_ref = client_reference_id.split(":", 1)
            if not job_id:
                job_id = str(parsed_job_id or "").strip()
            if not invoice_ref:
                invoice_ref = str(parsed_invoice_ref or "").strip()

    if (not job_id or not invoice_ref) and stripe_session_id:
        resolved_job_id, resolved_invoice_ref = _resolve_invoice_identifiers_from_session_id(db, stripe_session_id)
        if not job_id:
            job_id = resolved_job_id
        if not invoice_ref:
            invoice_ref = resolved_invoice_ref

    if not job_id or not invoice_ref or not ObjectId.is_valid(job_id):
        current_app.logger.warning(
            "Stripe checkout completion ignored due to missing metadata: session_id=%s job_id=%s invoice_ref=%s",
            stripe_session_id,
            job_id,
            invoice_ref,
        )
        return False

    amount_total = float(_stripe_obj_value(checkout_session, "amount_total", 0) or 0) / 100.0
    payment_intent_id = str(_stripe_obj_value(checkout_session, "payment_intent", "") or "").strip()
    return _finalize_invoice_payment(
        db,
        job_id,
        invoice_ref,
        stripe_session_id=stripe_session_id,
        payment_intent_id=payment_intent_id,
        amount_paid=amount_total,
    )


def _coerce_line_amount(value):
    return float(currency_to_float(value))


def _build_estimate_pricing_summary(estimate):
    estimate_doc = estimate or {}

    services_total = sum(_coerce_line_amount(service.get("price")) for service in (estimate_doc.get("services") or []))
    parts_total = sum(_coerce_line_amount(part.get("price")) for part in (estimate_doc.get("parts") or []))
    labors_total = sum(
        _coerce_line_amount(labor.get("line_total") or labor.get("hourly_rate"))
        for labor in (estimate_doc.get("labors") or [])
    )
    materials_total = sum(
        _coerce_line_amount(material.get("line_total") or material.get("price"))
        for material in (estimate_doc.get("materials") or [])
    )
    equipment_total = sum(
        _coerce_line_amount(equipment.get("line_total") or equipment.get("price"))
        for equipment in (estimate_doc.get("equipments") or [])
    )

    discounts_total = 0.0
    for discount in (estimate_doc.get("discounts") or []):
        line_value = _coerce_line_amount(discount.get("discount_amount") or discount.get("line_total"))
        discounts_total += abs(line_value)

    subtotal = services_total + parts_total + labors_total + materials_total + equipment_total
    pre_tax_total = max(0.0, subtotal - discounts_total)
    total_due = _coerce_line_amount(estimate_doc.get("total_amount"))
    tax_total = max(0.0, total_due - pre_tax_total)

    return {
        "subtotal": subtotal,
        "tax_total": tax_total,
        "discounts_total": discounts_total,
        "total_due": total_due,
        "subtotal_display": normalize_currency(subtotal),
        "tax_total_display": normalize_currency(tax_total),
        "discounts_total_display": normalize_currency(discounts_total),
        "total_due_display": normalize_currency(total_due),
    }


def _build_invoice_pricing_summary(job_doc):
    payload = job_doc or {}
    services_total = sum(_coerce_line_amount(service.get("price")) for service in (payload.get("services") or []))
    parts_total = sum(_coerce_line_amount(part.get("price")) for part in (payload.get("parts") or []))
    labors_total = sum(
        _coerce_line_amount(labor.get("line_total") or labor.get("hourly_rate"))
        for labor in (payload.get("labors") or [])
    )
    materials_total = sum(
        _coerce_line_amount(material.get("line_total") or material.get("price"))
        for material in (payload.get("materials") or [])
    )
    equipment_total = sum(
        _coerce_line_amount(equipment.get("line_total") or equipment.get("price"))
        for equipment in (payload.get("equipments") or [])
    )

    discounts_total = 0.0
    for discount in (payload.get("discounts") or []):
        line_value = _coerce_line_amount(discount.get("discount_amount") or discount.get("line_total"))
        discounts_total += abs(line_value)

    subtotal = services_total + parts_total + labors_total + materials_total + equipment_total
    pre_tax_total = max(0.0, subtotal - discounts_total)
    total_due = _coerce_line_amount(payload.get("total_amount"))
    tax_total = max(0.0, total_due - pre_tax_total)

    return {
        "subtotal": subtotal,
        "tax_total": tax_total,
        "discounts_total": discounts_total,
        "total_due": total_due,
        "subtotal_display": normalize_currency(subtotal),
        "tax_total_display": normalize_currency(tax_total),
        "discounts_total_display": normalize_currency(discounts_total),
        "total_due_display": normalize_currency(total_due),
    }


def _normalize_estimate_expiration_days(value, fallback=30):
    try:
        normalized = int(str(value or "").strip())
    except (TypeError, ValueError):
        normalized = int(fallback)
    return max(1, normalized)


def _resolve_default_estimate_expiration_days(db, fallback=30):
    business_id = resolve_current_business_id(db)
    if not business_id:
        return _normalize_estimate_expiration_days(fallback, fallback)

    business_doc = db.businesses.find_one({"_id": business_id}, {"default_estimate_expiration_days": 1})
    if not business_doc:
        return _normalize_estimate_expiration_days(fallback, fallback)

    return _normalize_estimate_expiration_days(business_doc.get("default_estimate_expiration_days"), fallback)


def _get_customer_properties(customer):
    raw_properties = (customer or {}).get("properties", [])
    if not isinstance(raw_properties, list):
        return []

    properties = []
    for prop in raw_properties:
        if not isinstance(prop, dict):
            continue
        properties.append(
            {
                "property_id": str(prop.get("property_id") or "").strip(),
                "property_name": str(prop.get("property_name") or "").strip(),
                "property_type": str(prop.get("property_type") or "").strip(),
                "address_line_1": str(prop.get("address_line_1") or "").strip(),
                "address_line_2": str(prop.get("address_line_2") or "").strip(),
                "city": str(prop.get("city") or "").strip(),
                "state": str(prop.get("state") or "").strip().upper(),
                "zip_code": str(prop.get("zip_code") or "").strip(),
                "is_default": bool(prop.get("is_default")),
            }
        )
    return properties


def _resolve_default_property(customer):
    properties = _get_customer_properties(customer)
    for prop in properties:
        if prop.get("is_default"):
            return prop
    return None


def _resolve_selected_property(customer, property_id):
    normalized_property_id = str(property_id or "").strip()
    if not normalized_property_id:
        return None

    for prop in _get_customer_properties(customer):
        if prop.get("property_id") == normalized_property_id:
            return prop
    return None


def _query_hvac_systems_for_property(db, customer_id, property_id):
    """Return a list of {id, title, system_type} dicts for HVAC systems on a property."""
    normalized_property_id = str(property_id or "").strip()
    if not customer_id or not normalized_property_id:
        return []
    query = {
        "$and": [
            build_reference_filter("customer_id", customer_id),
            build_reference_filter("property_id", normalized_property_id),
        ]
    }
    hvac_docs = list(db.hvacSystems.find(query).sort([("_id", -1)]))
    result = []
    for doc in hvac_docs:
        system_type = str(doc.get("system_type") or "").strip() or "HVAC System"
        result.append({
            "id": str(doc.get("_id")),
            "title": system_type,
            "system_type": system_type,
        })
    return result


def _apply_hvac_ids_to_components(components, form_field_values):
    """Patch hvac_system_ids onto each component dict from comma-separated form values."""
    for i, component in enumerate(components):
        raw = form_field_values[i] if i < len(form_field_values) else ""
        ids = [x.strip() for x in str(raw or "").split(",") if x.strip()]
        component["hvac_system_ids"] = ids
    return components


def _create_maintenance_records(db, job_id, job_doc, business_id):
    """Create one maintenanceRecord per HVAC system touched in a completed job."""
    property_id = str(job_doc.get("property_id") or "").strip()
    customer_id = job_doc.get("customer_id")
    completed_at = datetime.utcnow()
    date_completed = datetime.now().strftime("%m/%d/%Y")

    component_types = ["services", "parts", "labors", "materials", "equipments"]

    # Collect all referenced HVAC system IDs
    all_hvac_ids = set()
    for comp_type in component_types:
        for comp in (job_doc.get(comp_type) or []):
            for hvac_id in (comp.get("hvac_system_ids") or []):
                if hvac_id and str(hvac_id).strip():
                    all_hvac_ids.add(str(hvac_id).strip())

    if not all_hvac_ids:
        return

    records = []
    for hvac_system_id in all_hvac_ids:
        related_components = []
        for comp_type in component_types:
            for comp in (job_doc.get(comp_type) or []):
                if hvac_system_id in [str(x) for x in (comp.get("hvac_system_ids") or [])]:
                    related_components.append({
                        "component_type": comp_type,
                        "component": dict(comp),
                    })

        records.append({
            "job_id": str(job_id),
            "hvac_system_id": hvac_system_id,
            "property_id": property_id,
            "customer_id": customer_id,
            "business_id": business_id,
            "assigned_employee": str(job_doc.get("assigned_employee") or "").strip(),
            "date_completed": date_completed,
            "completed_at": completed_at,
            "scheduled_date": str(job_doc.get("scheduled_date") or "").strip(),
            "components": related_components,
            "total_amount": float(job_doc.get("total_amount") or 0.0),
            "address_line_1": str(job_doc.get("address_line_1") or "").strip(),
            "address_line_2": str(job_doc.get("address_line_2") or "").strip(),
            "city": str(job_doc.get("city") or "").strip(),
            "state": str(job_doc.get("state") or "").strip(),
            "zip_code": str(job_doc.get("zip_code") or "").strip(),
            "created_at": completed_at,
        })

    if records:
        db.maintenanceRecords.insert_many(records)


@bp.route("/api/hvac-systems-for-property")
def api_hvac_systems_for_property():
    if not _is_authenticated_employee():
        return jsonify({"error": "Unauthorized"}), 401
    db = ensure_connection_or_500()
    customer_id = request.args.get("customer_id", "").strip()
    property_id = request.args.get("property_id", "").strip()
    systems = _query_hvac_systems_for_property(db, customer_id, property_id)
    return jsonify({"hvac_systems": systems})


@bp.route("/jobs")
def jobs():
    db = ensure_connection_or_500()
    jobs_docs = list(db.jobs.find().sort([("scheduled_date", 1), ("date_created", -1), ("_id", -1)]))
    recurring_series_ids = {
        coerce_object_id(job.get("series_id"))
        for job in jobs_docs
        if str(job.get("job_kind") or "").strip() == "recurring_occurrence" and coerce_object_id(job.get("series_id")) is not None
    }
    recurring_series_ids.discard(None)

    recurring_status_by_series_id = {}
    if recurring_series_ids:
        recurring_series_docs = db.recurring_job_series.find(
            {"_id": {"$in": list(recurring_series_ids)}},
            {"status": 1},
        )
        recurring_status_by_series_id = {
            str(series.get("_id")): str(series.get("status") or "").strip()
            for series in recurring_series_docs
        }

    jobs_list = []
    for job_doc in jobs_docs:
        serialized = serialize_doc(job_doc)
        if str(serialized.get("job_kind") or "").strip() == "recurring_occurrence":
            series_key = str(serialized.get("series_id") or "").strip()
            serialized["recurring_series_status"] = recurring_status_by_series_id.get(series_key) or "Unknown"
        else:
            serialized["recurring_series_status"] = ""
        jobs_list.append(serialized)

    return render_template("jobs/jobs.html", jobs=jobs_list)


@bp.route("/jobs/export/csv")
def export_jobs_csv():
    db = ensure_connection_or_500()
    business_id = resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    jobs_rows = list(db.jobs.find(query).sort([("scheduled_date", 1), ("date_created", -1), ("_id", -1)]))
    return build_csv_export_response(
        jobs_rows,
        "jobs_export.csv",
        excluded_fields={"total"},
        field_transformers={"total_amount": normalize_currency},
    )


@bp.route("/estimates")
def estimates():
    db = ensure_connection_or_500()
    estimates_list = [
        serialize_doc(estimate)
        for estimate in db.estimates.find().sort([("created_at", -1), ("_id", -1)])
    ]

    normalized_estimates = []
    for estimate in estimates_list:
        estimate_doc = dict(estimate)
        estimate_doc["latest_file_path"] = resolve_estimate_file_path(estimate_doc)
        normalized_estimates.append(estimate_doc)

    return render_template("estimates/estimates.html", estimates=normalized_estimates)


@bp.route("/estimates/export/csv")
def export_estimates_csv():
    db = ensure_connection_or_500()
    business_id = resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    estimates_rows = list(db.estimates.find(query).sort([("created_at", -1), ("_id", -1)]))
    return build_csv_export_response(
        estimates_rows,
        "estimates_export.csv",
        excluded_fields={"total"},
        field_transformers={"total_amount": normalize_currency},
    )


@bp.route("/customers/<customerId>/jobs/create", methods=["GET", "POST"])
def create_job(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    if request.method == "POST":
        business_id = resolve_current_business_id(db)
        default_payment_due_days = _resolve_default_payment_due_days(db)
        selected_property_id = request.form.get("job_property_id", "").strip()
        if not selected_property_id:
            default_property = _resolve_default_property(customer)
            selected_property_id = str((default_property or {}).get("property_id") or "").strip()
        selected_property = _resolve_selected_property(customer, selected_property_id)
        selected_service_types = request.form.getlist("service_code[]") or request.form.getlist("service_type[]")
        entered_service_prices = request.form.getlist("service_price[]") or request.form.getlist("service_standard_price[]")
        entered_service_durations = request.form.getlist("service_hours[]") or request.form.getlist("service_estimated_hours[]") or request.form.getlist("service_duration[]")
        entered_service_emergency_calls = request.form.getlist("service_emergency_call[]")
        selected_part_names = request.form.getlist("part_code[]") or request.form.getlist("part_name[]")
        entered_part_prices = request.form.getlist("part_unit_cost[]") or request.form.getlist("part_price[]")
        selected_labor_descriptions = request.form.getlist("labor_description[]")
        entered_labor_hours = request.form.getlist("labor_hours[]")
        entered_labor_rates = request.form.getlist("labor_hourly_rate[]")
        selected_material_names = request.form.getlist("material_name[]")
        entered_material_quantities = request.form.getlist("material_quantity_used[]")
        entered_material_units = request.form.getlist("material_unit_of_measure[]")
        entered_material_prices = request.form.getlist("material_price[]")
        selected_equipment_names = request.form.getlist("equipment_name[]")
        entered_equipment_quantities = request.form.getlist("equipment_quantity_installed[]")
        entered_equipment_prices = request.form.getlist("equipment_price[]")
        selected_discount_names = request.form.getlist("discount_name[]")
        entered_discount_percentages = request.form.getlist("discount_percentage[]")
        entered_discount_amounts = request.form.getlist("discount_amount[]")
        service_query = {"business_id": business_id} if business_id else {"_id": None}
        part_query = {"business_id": business_id} if business_id else {"_id": None}
        labor_query = {"business_id": business_id} if business_id else {"_id": None}
        material_query = {"business_id": business_id} if business_id else {"_id": None}
        equipment_query = {"business_id": business_id} if business_id else {"_id": None}
        discount_query = {"business_id": business_id} if business_id else {"_id": None}
        service_docs = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
        part_docs = [serialize_doc(part) for part in db.parts.find(part_query).sort("part_name", 1)]
        labor_docs = [serialize_doc(labor) for labor in db.labors.find(labor_query).sort("labor_description", 1)]
        material_docs = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
        equipment_docs = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
        discount_docs = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
        service_catalog = build_service_catalog(service_docs)
        part_catalog = build_part_catalog(part_docs)
        labor_catalog = build_labor_catalog(labor_docs)
        material_catalog = build_material_catalog(material_docs)
        equipment_catalog = build_equipment_catalog(equipment_docs)
        discount_catalog = build_discount_catalog(discount_docs)
        services, services_total = build_job_services_from_form(
            selected_service_types,
            entered_service_prices,
            entered_service_durations,
            service_catalog,
            entered_service_emergency_calls,
        )
        parts, parts_total = build_job_parts_from_form(
            selected_part_names,
            entered_part_prices,
            part_catalog,
        )
        labors, labor_total = build_job_labors_from_form(
            selected_labor_descriptions,
            entered_labor_hours,
            entered_labor_rates,
            labor_catalog,
        )
        materials, materials_total = build_job_materials_from_form(
            selected_material_names,
            entered_material_quantities,
            entered_material_units,
            entered_material_prices,
            material_catalog,
        )
        equipments, equipment_total = build_job_equipments_from_form(
            selected_equipment_names,
            entered_equipment_quantities,
            entered_equipment_prices,
            equipment_catalog,
        )
        discounts, discounts_total = build_job_discounts_from_form(
            selected_discount_names,
            entered_discount_percentages,
            entered_discount_amounts,
            discount_catalog,
        )
        _apply_hvac_ids_to_components(services, request.form.getlist("service_hvac_system_ids[]"))
        _apply_hvac_ids_to_components(parts, request.form.getlist("part_hvac_system_ids[]"))
        _apply_hvac_ids_to_components(labors, request.form.getlist("labor_hvac_system_ids[]"))
        _apply_hvac_ids_to_components(materials, request.form.getlist("material_hvac_system_ids[]"))
        _apply_hvac_ids_to_components(equipments, request.form.getlist("equipment_hvac_system_ids[]"))
        total = services_total + parts_total + labor_total + materials_total + equipment_total - discounts_total

        primary_service = services[0]["type"] if services else "No services added."
        scheduled_date = format_date(request.form.get("job_date", ""))
        scheduled_time = request.form.get("job_time", "").strip()
        payment_due_days = _normalize_payment_due_days(
            request.form.get("payment_due_days", ""),
            default_payment_due_days,
        )
        payment_due_days_offset = payment_due_days
        date_scheduled = datetime.now().strftime("%m/%d/%Y") if (scheduled_date and scheduled_time) else ""
        job_status = resolve_job_status(scheduled_date, scheduled_time, services, parts, labors, materials, equipments, discounts)

        assigned_employee = request.form.get("job_assigned_employee", "").strip()
        recurring_data = _parse_recurrence_request(request, scheduled_date, scheduled_time)
        invoice_notes = request.form.get("invoice_notes", "").strip()

        if recurring_data.get("is_recurring"):
            series_doc = _build_recurring_series_document(
                customer,
                business_id,
                selected_property,
                selected_property_id,
                primary_service,
                services,
                parts,
                labors,
                materials,
                equipments,
                discounts,
                total,
                assigned_employee,
                recurring_data,
                scheduled_date,
                scheduled_time,
                payment_due_days_offset,
                request,
            )
            series_inserted = db.recurring_job_series.insert_one(series_doc)
            series_doc["_id"] = series_inserted.inserted_id
            created_occurrence_id = _create_occurrence_from_series(db, series_doc, scheduled_date, 1)
            if created_occurrence_id:
                current_app.logger.info("Recurring job series created: series_id=%s occurrence_id=%s customer_id=%s by employee_id=%s", str(series_inserted.inserted_id), created_occurrence_id, customerId, session.get("employee_id"))
                return redirect(url_for("jobs.view_job", jobId=created_occurrence_id))

        new_job = {
            "customer_id": reference_value(customerId),
            "customer_name": f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip(),
            "company": customer.get("company", ""),
            "property_id": selected_property_id if selected_property else "",
            "property_name": (selected_property or {}).get("property_name") or "",
            "job_type": primary_service,
            "services": services,
            "parts": parts,
            "labors": labors,
            "materials": materials,
            "equipments": equipments,
            "discounts": discounts,
            "status": job_status,
            "scheduled_date": scheduled_date,
            "scheduled_time": scheduled_time,
            "dateScheduled": date_scheduled,
            "address_line_1": request.form.get("job_address_line_1", "").strip(),
            "address_line_2": request.form.get("job_address_line_2", "").strip(),
            "city": request.form.get("job_city", "").strip(),
            "state": request.form.get("job_state", "").strip().upper(),
            "zip_code": request.form.get("job_zip_code", "").strip(),
            "assigned_employee": assigned_employee,
            "total_amount": float(total or 0.0),
            "invoice_notes": invoice_notes,
            "payment_due_days": payment_due_days,
            "internal_notes": [],
            "date_created": datetime.now().strftime("%m/%d/%Y"),
            "created_at": datetime.utcnow(),
            "invoices": [],
            "business_id": business_id,
            "job_kind": "one_time",
            "series_id": None,
            "occurrence_index": None,
            "recurrence_summary": "",
        }
        inserted = db.jobs.insert_one(new_job)
        current_app.logger.info("Job created: id=%s customer_id=%s by employee_id=%s", str(inserted.inserted_id), customerId, session.get("employee_id"))
        return redirect(url_for("jobs.view_job", jobId=str(inserted.inserted_id)))

    business_id = resolve_current_business_id(db)
    service_query = {"business_id": business_id} if business_id else {"_id": None}
    part_query = {"business_id": business_id} if business_id else {"_id": None}
    labor_query = {"business_id": business_id} if business_id else {"_id": None}
    material_query = {"business_id": business_id} if business_id else {"_id": None}
    equipment_query = {"business_id": business_id} if business_id else {"_id": None}
    discount_query = {"business_id": business_id} if business_id else {"_id": None}
    services = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
    parts = [serialize_doc(part) for part in db.parts.find(part_query).sort("part_name", 1)]
    labors = [serialize_doc(labor) for labor in db.labors.find(labor_query).sort("labor_description", 1)]
    materials = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
    equipments = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
    discounts = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
    employee_options = build_employee_options(db)
    services_catalog = build_service_catalog(services)
    parts_catalog = build_part_catalog(parts)
    labors_catalog = build_labor_catalog(labors)
    materials_catalog = build_material_catalog(materials)
    equipments_catalog = build_equipment_catalog(equipments)
    discounts_catalog = build_discount_catalog(discounts)
    parts_by_id = {p["_id"]: p["part_code"] for p in parts}
    materials_by_id = {m["_id"]: m["material_name"] for m in materials}
    default_payment_due_days = _resolve_default_payment_due_days(db)
    payment_due_days_value = default_payment_due_days
    default_property = _resolve_default_property(customer)
    selected_property_id = str((default_property or {}).get("property_id") or "").strip()
    initial_address_line_1 = (default_property or {}).get("address_line_1") or customer.get("address_line_1", "")
    initial_address_line_2 = (default_property or {}).get("address_line_2") or customer.get("address_line_2", "")
    initial_city = (default_property or {}).get("city") or customer.get("city", "")
    initial_state = (default_property or {}).get("state") or customer.get("state", "")
    initial_zip_code = (default_property or {}).get("zip_code") or customer.get("zip_code", "")
    initial_hvac_systems = _query_hvac_systems_for_property(db, customerId, selected_property_id)

    return render_template(
        "jobs/create_job.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        customer_properties=_get_customer_properties(customer),
        selected_property_id=selected_property_id,
        initial_address_line_1=initial_address_line_1,
        initial_address_line_2=initial_address_line_2,
        initial_city=initial_city,
        initial_state=initial_state,
        initial_zip_code=initial_zip_code,
        initial_hvac_systems=initial_hvac_systems,
        services=services,
        parts=parts,
        labors=labors,
        materials=materials,
        equipments=equipments,
        discounts=discounts,
        employee_options=employee_options,
        services_catalog=services_catalog,
        parts_catalog=parts_catalog,
        labors_catalog=labors_catalog,
        materials_catalog=materials_catalog,
        equipments_catalog=equipments_catalog,
        discounts_catalog=discounts_catalog,
        parts_by_id=parts_by_id,
        materials_by_id=materials_by_id,
        payment_due_days_value=payment_due_days_value,
        recurring_frequency_options=RECURRING_FREQUENCY_OPTIONS,
        recurring_end_type_options=RECURRING_END_TYPE_OPTIONS,
        recurrence_state=_build_recurrence_form_state(),
        recurrence_locked=False,
    )


@bp.route("/customers/<customerId>/estimates/create", methods=["GET", "POST"])
def create_estimate(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    if request.method == "POST":
        business_id = resolve_current_business_id(db)
        default_estimate_expiration_days = _resolve_default_estimate_expiration_days(db)
        selected_property_id = request.form.get("job_property_id", "").strip()
        if not selected_property_id:
            default_property = _resolve_default_property(customer)
            selected_property_id = str((default_property or {}).get("property_id") or "").strip()
        selected_property = _resolve_selected_property(customer, selected_property_id)
        selected_service_types = request.form.getlist("service_code[]") or request.form.getlist("service_type[]")
        entered_service_prices = request.form.getlist("service_price[]") or request.form.getlist("service_standard_price[]")
        entered_service_durations = request.form.getlist("service_hours[]") or request.form.getlist("service_estimated_hours[]") or request.form.getlist("service_duration[]")
        entered_service_emergency_calls = request.form.getlist("service_emergency_call[]")
        selected_part_names = request.form.getlist("part_code[]") or request.form.getlist("part_name[]")
        entered_part_prices = request.form.getlist("part_unit_cost[]") or request.form.getlist("part_price[]")
        selected_labor_descriptions = request.form.getlist("labor_description[]")
        entered_labor_hours = request.form.getlist("labor_hours[]")
        entered_labor_rates = request.form.getlist("labor_hourly_rate[]")
        selected_material_names = request.form.getlist("material_name[]")
        entered_material_quantities = request.form.getlist("material_quantity_used[]")
        entered_material_units = request.form.getlist("material_unit_of_measure[]")
        entered_material_prices = request.form.getlist("material_price[]")
        selected_equipment_names = request.form.getlist("equipment_name[]")
        entered_equipment_quantities = request.form.getlist("equipment_quantity_installed[]")
        entered_equipment_prices = request.form.getlist("equipment_price[]")
        selected_discount_names = request.form.getlist("discount_name[]")
        entered_discount_percentages = request.form.getlist("discount_percentage[]")
        entered_discount_amounts = request.form.getlist("discount_amount[]")

        service_query = {"business_id": business_id} if business_id else {"_id": None}
        part_query = {"business_id": business_id} if business_id else {"_id": None}
        labor_query = {"business_id": business_id} if business_id else {"_id": None}
        material_query = {"business_id": business_id} if business_id else {"_id": None}
        equipment_query = {"business_id": business_id} if business_id else {"_id": None}
        discount_query = {"business_id": business_id} if business_id else {"_id": None}
        service_docs = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
        part_docs = [serialize_doc(part) for part in db.parts.find(part_query).sort("part_name", 1)]
        labor_docs = [serialize_doc(labor) for labor in db.labors.find(labor_query).sort("labor_description", 1)]
        material_docs = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
        equipment_docs = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
        discount_docs = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
        service_catalog = build_service_catalog(service_docs)
        part_catalog = build_part_catalog(part_docs)
        labor_catalog = build_labor_catalog(labor_docs)
        material_catalog = build_material_catalog(material_docs)
        equipment_catalog = build_equipment_catalog(equipment_docs)
        discount_catalog = build_discount_catalog(discount_docs)

        services, services_total = build_job_services_from_form(
            selected_service_types,
            entered_service_prices,
            entered_service_durations,
            service_catalog,
            entered_service_emergency_calls,
        )
        parts, parts_total = build_job_parts_from_form(
            selected_part_names,
            entered_part_prices,
            part_catalog,
        )
        labors, labor_total = build_job_labors_from_form(
            selected_labor_descriptions,
            entered_labor_hours,
            entered_labor_rates,
            labor_catalog,
        )
        materials, materials_total = build_job_materials_from_form(
            selected_material_names,
            entered_material_quantities,
            entered_material_units,
            entered_material_prices,
            material_catalog,
        )
        equipments, equipment_total = build_job_equipments_from_form(
            selected_equipment_names,
            entered_equipment_quantities,
            entered_equipment_prices,
            equipment_catalog,
        )
        discounts, discounts_total = build_job_discounts_from_form(
            selected_discount_names,
            entered_discount_percentages,
            entered_discount_amounts,
            discount_catalog,
        )
        total = services_total + parts_total + labor_total + materials_total + equipment_total - discounts_total

        estimated_by_employee = request.form.get("job_assigned_employee", "").strip()
        estimate_notes = request.form.get("estimate_notes", "").strip()
        proposed_job_date = format_date(request.form.get("proposed_job_date", ""))
        proposed_job_time = request.form.get("proposed_job_time", "").strip()
        recurrence_data = _parse_recurrence_request(request, proposed_job_date, proposed_job_time)
        estimate_expiration_days = _normalize_estimate_expiration_days(
            request.form.get("estimate_expiration_days", ""),
            default_estimate_expiration_days,
        )

        new_estimate = {
            "customer_id": reference_value(customerId),
            "customer_name": f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip(),
            "company": customer.get("company", ""),
            "property_id": selected_property_id if selected_property else "",
            "property_name": (selected_property or {}).get("property_name") or "",
            "services": services,
            "parts": parts,
            "equipments": equipments,
            "labors": labors,
            "materials": materials,
            "discounts": discounts,
            "status": "Created",
            "date_created": datetime.now().strftime("%m/%d/%Y"),
            "time_created": datetime.now().strftime("%H:%M:%S"),
            "date_sent": "",
            "time_sent": "",
            "date_accepted": "",
            "time_accepted": "",
            "date_declined": "",
            "time_declined": "",
            "address_line_1": request.form.get("job_address_line_1", "").strip(),
            "address_line_2": request.form.get("job_address_line_2", "").strip(),
            "city": request.form.get("job_city", "").strip(),
            "state": request.form.get("job_state", "").strip().upper(),
            "zip_code": request.form.get("job_zip_code", "").strip(),
            "created_by_employee": (session.get("employee_name") or "").strip(),
            "estimated_by_employee": estimated_by_employee,
            "proposed_job_date": proposed_job_date,
            "proposed_job_time": proposed_job_time,
            "job_schedule_type": recurrence_data.get("schedule_type") or "one_time",
            "recurring_frequency": recurrence_data.get("frequency") or "",
            "recurring_end_type": recurrence_data.get("end_type") or "never",
            "recurring_end_date": recurrence_data.get("end_date") or "",
            "recurring_end_after": recurrence_data.get("max_occurrences"),
            "recurrence_summary": recurrence_data.get("summary") or "",
            "total_amount": float(total or 0.0),
            "estimate_notes": estimate_notes,
            "estimate_expiration_days": estimate_expiration_days,
            "file_path": [],
            "latest_file_path": "",
            "created_at": datetime.utcnow(),
        }

        inserted = db.estimates.insert_one(new_estimate)
        estimate_id = str(inserted.inserted_id)

        try:
            business_logo_path = resolve_current_business_logo_path(db)
            business_payload = {}
            business_id = resolve_current_business_id(db)
            if business_id:
                business_doc = db.businesses.find_one(
                    {"_id": business_id},
                    {
                        "company_name": 1,
                        "business_name": 1,
                        "address_line_1": 1,
                        "address_line_2": 1,
                        "city": 1,
                        "state": 1,
                        "zip_code": 1,
                        "phone_number": 1,
                        "fax_number": 1,
                        "email": 1,
                        "website": 1,
                        "license_number": 1,
                        "warranty_info": 1,
                    },
                )
                if business_doc:
                    business_payload = serialize_doc(business_doc)
            estimate_pdf_path = generate_estimate(
                estimate_id,
                serialize_estimate_for_pdf(new_estimate),
                serialize_doc(customer),
                business_logo_path=business_logo_path,
                business=business_payload,
            )
            filename = os.path.basename(estimate_pdf_path)
            public_file_path = url_for("download_invoice", filename=filename)

            db.estimates.update_one(
                {"_id": ObjectId(estimate_id)},
                {
                    "$set": {
                        "file_path": [{"file_path": public_file_path}],
                        "latest_file_path": public_file_path,
                    }
                },
            )
        except Exception as exc:
            db.estimates.delete_one({"_id": ObjectId(estimate_id)})
            current_app.logger.error("Estimate create rollback: id=%s customer_id=%s error=%s", estimate_id, customerId, exc)
            return redirect(url_for("customers.view_customer", customerId=customerId))

        current_app.logger.info("Estimate created: id=%s customer_id=%s by employee_id=%s", estimate_id, customerId, session.get("employee_id"))
        return redirect(url_for("jobs.view_estimate", estimateId=estimate_id))

    business_id = resolve_current_business_id(db)
    default_estimate_expiration_days = _resolve_default_estimate_expiration_days(db)
    service_query = {"business_id": business_id} if business_id else {"_id": None}
    part_query = {"business_id": business_id} if business_id else {"_id": None}
    labor_query = {"business_id": business_id} if business_id else {"_id": None}
    material_query = {"business_id": business_id} if business_id else {"_id": None}
    equipment_query = {"business_id": business_id} if business_id else {"_id": None}
    discount_query = {"business_id": business_id} if business_id else {"_id": None}
    services = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
    parts = [serialize_doc(part) for part in db.parts.find(part_query).sort("part_name", 1)]
    labors = [serialize_doc(labor) for labor in db.labors.find(labor_query).sort("labor_description", 1)]
    materials = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
    equipments = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
    discounts = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
    employee_options = build_employee_options(db)
    services_catalog = build_service_catalog(services)
    parts_catalog = build_part_catalog(parts)
    labors_catalog = build_labor_catalog(labors)
    materials_catalog = build_material_catalog(materials)
    equipments_catalog = build_equipment_catalog(equipments)
    discounts_catalog = build_discount_catalog(discounts)
    parts_by_id = {p["_id"]: p["part_code"] for p in parts}
    materials_by_id = {m["_id"]: m["material_name"] for m in materials}
    default_property = _resolve_default_property(customer)
    selected_property_id = str((default_property or {}).get("property_id") or "").strip()
    customer_for_view = serialize_doc(customer)
    if default_property:
        customer_for_view["address_line_1"] = (default_property or {}).get("address_line_1", "")
        customer_for_view["address_line_2"] = (default_property or {}).get("address_line_2", "")
        customer_for_view["city"] = (default_property or {}).get("city", "")
        customer_for_view["state"] = (default_property or {}).get("state", "")
        customer_for_view["zip_code"] = (default_property or {}).get("zip_code", "")

    return render_template(
        "estimates/create_estimate.html",
        customerId=customerId,
        customer=customer_for_view,
        customer_properties=_get_customer_properties(customer),
        selected_property_id=selected_property_id,
        services=services,
        parts=parts,
        labors=labors,
        materials=materials,
        equipments=equipments,
        discounts=discounts,
        employee_options=employee_options,
        services_catalog=services_catalog,
        parts_catalog=parts_catalog,
        labors_catalog=labors_catalog,
        materials_catalog=materials_catalog,
        equipments_catalog=equipments_catalog,
        discounts_catalog=discounts_catalog,
        parts_by_id=parts_by_id,
        materials_by_id=materials_by_id,
        estimate_expiration_days=default_estimate_expiration_days,
        proposed_job_date_value="",
        proposed_job_time_value="",
        recurring_frequency_options=RECURRING_FREQUENCY_OPTIONS,
        recurring_end_type_options=RECURRING_END_TYPE_OPTIONS,
        recurrence_state=_build_estimate_recurrence_form_state(),
    )


@bp.route("/estimates/<estimateId>")
def view_estimate(estimateId):
    db = ensure_connection_or_500()
    estimate = db.estimates.find_one({"_id": object_id_or_404(estimateId)})
    if not estimate:
        return redirect(url_for("home"))

    token_value = str(request.args.get("token") or "").strip()
    is_staff_view = _is_authenticated_employee()
    has_customer_token = _verify_estimate_access_token(estimate, token_value)
    if not is_staff_view and not has_customer_token:
        return redirect(url_for("auth.login"))

    quote_email_template = ""
    employee_id = session.get("employee_id")
    if is_staff_view and employee_id and ObjectId.is_valid(employee_id):
        employee = db.employees.find_one({"_id": ObjectId(employee_id)})
        if employee:
            business_ref = employee.get("business")
            business_oid = None
            if isinstance(business_ref, ObjectId):
                business_oid = business_ref
            elif isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
                business_oid = ObjectId(business_ref)
            if business_oid:
                business = db.businesses.find_one({"_id": business_oid})
                if business:
                    quote_email_template = business.get("quote_email_template", "")

    customer = {}
    customer_id = estimate.get("customer_id")
    if customer_id:
        customer_doc = db.customers.find_one(build_reference_filter("_id", customer_id))
        if customer_doc:
            customer = serialize_doc(customer_doc)

    estimate_doc = serialize_doc(estimate)
    estimate_doc["latest_file_path"] = resolve_estimate_file_path(estimate_doc)
    estimate_doc["proposed_job_date_iso"] = _mmddyyyy_to_iso_date(estimate_doc.get("proposed_job_date"))
    estimate_doc["date_created_iso"] = _mmddyyyy_to_iso_date(estimate_doc.get("date_created"))
    estimate_doc["date_sent_iso"] = _mmddyyyy_to_iso_date(estimate_doc.get("date_sent"))
    estimate_doc["date_accepted_iso"] = _mmddyyyy_to_iso_date(estimate_doc.get("date_accepted"))
    estimate_doc["date_declined_iso"] = _mmddyyyy_to_iso_date(estimate_doc.get("date_declined"))
    estimate_doc["job_schedule_type"] = "recurring" if str(estimate_doc.get("job_schedule_type") or "").strip() == "recurring" else "one_time"
    estimate_doc["job_schedule_type_label"] = "Recurring" if estimate_doc["job_schedule_type"] == "recurring" else "One-Time"
    estimate_doc["recurring_end_type_label"] = RECURRING_END_TYPE_LABELS.get(
        str(estimate_doc.get("recurring_end_type") or "").strip(),
        "Never",
    )
    if estimate_doc["job_schedule_type"] == "recurring" and not str(estimate_doc.get("recurrence_summary") or "").strip():
        estimate_doc["recurrence_summary"] = _build_recurrence_summary(estimate_doc.get("recurring_frequency"))
    accepted_signature_date_iso, accepted_signature_time, accepted_signature_date_display = _iso_datetime_to_utc_parts(
        estimate_doc.get("accepted_signature_captured_at")
    )
    estimate_doc["accepted_signature_captured_date_iso"] = accepted_signature_date_iso
    estimate_doc["accepted_signature_captured_time"] = accepted_signature_time
    estimate_doc["accepted_signature_captured_date_display"] = accepted_signature_date_display
    pricing_summary = _build_estimate_pricing_summary(estimate_doc)

    return render_template(
        "estimates/view_estimate.html",
        estimateId=estimateId,
        estimate=estimate_doc,
        customer=customer,
        quote_email_template=quote_email_template,
        is_staff_view=is_staff_view,
        access_token=token_value,
        pricing_summary=pricing_summary,
    )


@bp.route("/estimates/<estimateId>/update", methods=["GET", "POST"])
def update_estimate(estimateId):
    db = ensure_connection_or_500()
    estimate = db.estimates.find_one({"_id": object_id_or_404(estimateId)})
    if not estimate:
        return redirect(url_for("home"))

    if request.method == "POST":
        business_id = resolve_current_business_id(db)
        default_estimate_expiration_days = _resolve_default_estimate_expiration_days(db)
        selected_property_id = request.form.get("job_property_id", "").strip()
        customer_for_property = {}
        estimate_customer_id = estimate.get("customer_id")
        if estimate_customer_id:
            customer_for_property = db.customers.find_one(build_reference_filter("_id", estimate_customer_id)) or {}
        if not selected_property_id:
            default_property = _resolve_default_property(customer_for_property)
            selected_property_id = str((default_property or {}).get("property_id") or "").strip()
        selected_property = _resolve_selected_property(customer_for_property, selected_property_id)
        selected_service_types = request.form.getlist("service_code[]") or request.form.getlist("service_type[]")
        entered_service_prices = request.form.getlist("service_price[]") or request.form.getlist("service_standard_price[]")
        entered_service_durations = request.form.getlist("service_hours[]") or request.form.getlist("service_estimated_hours[]") or request.form.getlist("service_duration[]")
        entered_service_emergency_calls = request.form.getlist("service_emergency_call[]")
        selected_part_names = request.form.getlist("part_code[]") or request.form.getlist("part_name[]")
        entered_part_prices = request.form.getlist("part_unit_cost[]") or request.form.getlist("part_price[]")
        selected_labor_descriptions = request.form.getlist("labor_description[]")
        entered_labor_hours = request.form.getlist("labor_hours[]")
        entered_labor_rates = request.form.getlist("labor_hourly_rate[]")
        selected_material_names = request.form.getlist("material_name[]")
        entered_material_quantities = request.form.getlist("material_quantity_used[]")
        entered_material_units = request.form.getlist("material_unit_of_measure[]")
        entered_material_prices = request.form.getlist("material_price[]")
        selected_equipment_names = request.form.getlist("equipment_name[]")
        entered_equipment_quantities = request.form.getlist("equipment_quantity_installed[]")
        entered_equipment_prices = request.form.getlist("equipment_price[]")
        selected_discount_names = request.form.getlist("discount_name[]")
        entered_discount_percentages = request.form.getlist("discount_percentage[]")
        entered_discount_amounts = request.form.getlist("discount_amount[]")

        service_query = {"business_id": business_id} if business_id else {"_id": None}
        part_query = {"business_id": business_id} if business_id else {"_id": None}
        labor_query = {"business_id": business_id} if business_id else {"_id": None}
        material_query = {"business_id": business_id} if business_id else {"_id": None}
        equipment_query = {"business_id": business_id} if business_id else {"_id": None}
        discount_query = {"business_id": business_id} if business_id else {"_id": None}
        service_docs = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
        part_docs = [serialize_doc(part) for part in db.parts.find(part_query).sort("part_name", 1)]
        labor_docs = [serialize_doc(labor) for labor in db.labors.find(labor_query).sort("labor_description", 1)]
        material_docs = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
        equipment_docs = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
        discount_docs = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
        service_catalog = build_service_catalog(service_docs)
        part_catalog = build_part_catalog(part_docs)
        labor_catalog = build_labor_catalog(labor_docs)
        material_catalog = build_material_catalog(material_docs)
        equipment_catalog = build_equipment_catalog(equipment_docs)
        discount_catalog = build_discount_catalog(discount_docs)

        services, services_total = build_job_services_from_form(
            selected_service_types,
            entered_service_prices,
            entered_service_durations,
            service_catalog,
            entered_service_emergency_calls,
        )
        parts, parts_total = build_job_parts_from_form(
            selected_part_names,
            entered_part_prices,
            part_catalog,
        )
        labors, labor_total = build_job_labors_from_form(
            selected_labor_descriptions,
            entered_labor_hours,
            entered_labor_rates,
            labor_catalog,
        )
        materials, materials_total = build_job_materials_from_form(
            selected_material_names,
            entered_material_quantities,
            entered_material_units,
            entered_material_prices,
            material_catalog,
        )
        equipments, equipment_total = build_job_equipments_from_form(
            selected_equipment_names,
            entered_equipment_quantities,
            entered_equipment_prices,
            equipment_catalog,
        )
        discounts, discounts_total = build_job_discounts_from_form(
            selected_discount_names,
            entered_discount_percentages,
            entered_discount_amounts,
            discount_catalog,
        )
        total = services_total + parts_total + labor_total + materials_total + equipment_total - discounts_total

        estimated_by_employee = request.form.get("job_assigned_employee", "").strip()
        estimate_notes = request.form.get("estimate_notes", "").strip()
        proposed_job_date = format_date(request.form.get("proposed_job_date", ""))
        proposed_job_time = request.form.get("proposed_job_time", "").strip()
        recurrence_data = _parse_recurrence_request(request, proposed_job_date, proposed_job_time)
        estimate_expiration_days = _normalize_estimate_expiration_days(
            request.form.get("estimate_expiration_days", ""),
            default_estimate_expiration_days,
        )

        updated_data = {
            "services": services,
            "parts": parts,
            "labors": labors,
            "materials": materials,
            "equipments": equipments,
            "discounts": discounts,
            "property_id": selected_property_id if selected_property else "",
            "property_name": (selected_property or {}).get("property_name") or "",
            "address_line_1": request.form.get("job_address_line_1", "").strip(),
            "address_line_2": request.form.get("job_address_line_2", "").strip(),
            "city": request.form.get("job_city", "").strip(),
            "state": request.form.get("job_state", "").strip().upper(),
            "zip_code": request.form.get("job_zip_code", "").strip(),
            "estimated_by_employee": estimated_by_employee,
            "proposed_job_date": proposed_job_date,
            "proposed_job_time": proposed_job_time,
            "job_schedule_type": recurrence_data.get("schedule_type") or "one_time",
            "recurring_frequency": recurrence_data.get("frequency") or "",
            "recurring_end_type": recurrence_data.get("end_type") or "never",
            "recurring_end_date": recurrence_data.get("end_date") or "",
            "recurring_end_after": recurrence_data.get("max_occurrences"),
            "recurrence_summary": recurrence_data.get("summary") or "",
            "estimate_notes": estimate_notes,
            "estimate_expiration_days": estimate_expiration_days,
            "total_amount": float(total or 0.0),
            "date_updated": datetime.now().strftime("%m/%d/%Y"),
            "time_updated": datetime.now().strftime("%H:%M:%S"),
            "updated_at": datetime.utcnow(),
        }

        estimate_for_pdf = dict(estimate)
        estimate_for_pdf.update(updated_data)
        customer = {}
        customer_id = estimate.get("customer_id")
        if customer_id:
            customer_doc = db.customers.find_one(build_reference_filter("_id", customer_id))
            if customer_doc:
                customer = serialize_doc(customer_doc)

        business_logo_path = resolve_current_business_logo_path(db)
        business_payload = {}
        business_id = resolve_current_business_id(db)
        if business_id:
            business_doc = db.businesses.find_one(
                {"_id": business_id},
                {
                    "company_name": 1,
                    "business_name": 1,
                    "address_line_1": 1,
                    "address_line_2": 1,
                    "city": 1,
                    "state": 1,
                    "zip_code": 1,
                    "phone_number": 1,
                    "fax_number": 1,
                    "email": 1,
                    "website": 1,
                    "license_number": 1,
                    "warranty_info": 1,
                },
            )
            if business_doc:
                business_payload = serialize_doc(business_doc)
        previous_file_path = resolve_estimate_file_path(estimate)
        estimate_pdf_path = generate_estimate(
            estimateId,
            serialize_estimate_for_pdf(estimate_for_pdf),
            customer,
            business_logo_path=business_logo_path,
            business=business_payload,
        )
        filename = os.path.basename(estimate_pdf_path)
        public_file_path = url_for("download_invoice", filename=filename)

        previous_file_abs = estimate_pdf_absolute_path_from_url(previous_file_path)
        if previous_file_abs and os.path.abspath(estimate_pdf_path) != previous_file_abs:
            remove_estimate_pdf_file(previous_file_path)
        elif previous_file_abs:
            current_app.logger.warning("Skipped deleting previous estimate PDF because it matched new file path: %s", previous_file_abs)

        updated_data["file_path"] = [{"file_path": public_file_path}]
        updated_data["latest_file_path"] = public_file_path

        db.estimates.update_one({"_id": ObjectId(estimateId)}, {"$set": updated_data})
        return redirect(url_for("jobs.view_estimate", estimateId=estimateId))

    customer = {}
    customer_id = estimate.get("customer_id")
    if customer_id:
        customer_doc = db.customers.find_one(build_reference_filter("_id", customer_id))
        if customer_doc:
            customer = serialize_doc(customer_doc)

    business_id = resolve_current_business_id(db)
    service_query = {"business_id": business_id} if business_id else {"_id": None}
    part_query = {"business_id": business_id} if business_id else {"_id": None}
    labor_query = {"business_id": business_id} if business_id else {"_id": None}
    material_query = {"business_id": business_id} if business_id else {"_id": None}
    equipment_query = {"business_id": business_id} if business_id else {"_id": None}
    discount_query = {"business_id": business_id} if business_id else {"_id": None}
    services = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
    parts = [serialize_doc(part) for part in db.parts.find(part_query).sort("part_name", 1)]
    labors = [serialize_doc(labor) for labor in db.labors.find(labor_query).sort("labor_description", 1)]
    materials = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
    equipments = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
    discounts = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
    employee_options = build_employee_options(db)
    services_catalog = build_service_catalog(services)
    parts_catalog = build_part_catalog(parts)
    labors_catalog = build_labor_catalog(labors)
    materials_catalog = build_material_catalog(materials)
    equipments_catalog = build_equipment_catalog(equipments)
    discounts_catalog = build_discount_catalog(discounts)
    parts_by_id = {p["_id"]: p["part_code"] for p in parts}
    materials_by_id = {m["_id"]: m["material_name"] for m in materials}

    estimate_doc = serialize_doc(estimate)
    estimate_doc["file_path"] = normalize_estimate_file_history(estimate_doc.get("file_path"))
    default_estimate_expiration_days = _resolve_default_estimate_expiration_days(db)
    estimate_expiration_days = _normalize_estimate_expiration_days(
        estimate_doc.get("estimate_expiration_days"),
        default_estimate_expiration_days,
    )
    selected_property_id = str(estimate_doc.get("property_id") or "").strip()
    if not selected_property_id:
        default_property = _resolve_default_property(customer)
        selected_property_id = str((default_property or {}).get("property_id") or "").strip()
    proposed_job_date_value = _mmddyyyy_to_iso_date(estimate_doc.get("proposed_job_date"))
    proposed_job_time_value = str(estimate_doc.get("proposed_job_time") or "").strip()
    recurrence_state = _build_estimate_recurrence_form_state(estimate_doc)

    return render_template(
        "estimates/update_estimate.html",
        estimateId=estimateId,
        estimate=estimate_doc,
        jobId=estimateId,
        job=estimate_doc,
        customer=customer,
        customer_properties=_get_customer_properties(customer),
        selected_property_id=selected_property_id,
        services=services,
        parts=parts,
        labors=labors,
        materials=materials,
        equipments=equipments,
        discounts=discounts,
        employee_options=employee_options,
        services_catalog=services_catalog,
        parts_catalog=parts_catalog,
        labors_catalog=labors_catalog,
        materials_catalog=materials_catalog,
        equipments_catalog=equipments_catalog,
        discounts_catalog=discounts_catalog,
        parts_by_id=parts_by_id,
        materials_by_id=materials_by_id,
        estimate_expiration_days=estimate_expiration_days,
        proposed_job_date_value=proposed_job_date_value,
        proposed_job_time_value=proposed_job_time_value,
        recurring_frequency_options=RECURRING_FREQUENCY_OPTIONS,
        recurring_end_type_options=RECURRING_END_TYPE_OPTIONS,
        recurrence_state=recurrence_state,
    )


@bp.route("/estimates/<estimateId>/email", methods=["POST"])
def send_estimate_email_by_id(estimateId):
    db = ensure_connection_or_500()
    estimate = db.estimates.find_one({"_id": object_id_or_404(estimateId)})
    if not estimate:
        return jsonify({"success": False, "error": "Estimate not found"}), 404

    try:
        data = request.get_json() or {}
        recipient_email = data.get("recipient_email", "")
        subject = data.get("subject", "")
        body = data.get("body", "")
        estimate_file = data.get("estimate_file", "") or resolve_estimate_file_path(estimate)

        if not recipient_email or not subject or not body:
            return jsonify({"success": False, "error": "Missing required fields"}), 400

        filename = estimate_file.split("/")[-1]
        base_dir = os.path.dirname(os.path.dirname(__file__))
        invoices_dir = os.path.join(base_dir, "invoices")
        filepath = os.path.join(invoices_dir, filename)

        if not os.path.exists(filepath) or not os.path.abspath(filepath).startswith(os.path.abspath(invoices_dir)):
            return jsonify({"success": False, "error": "Estimate file not found"}), 404

        access_token = _issue_estimate_access_token(db, estimateId, recipient_email)
        estimate_link = _build_estimate_view_url(estimateId, access_token=access_token, external=True)
        appended_body = (
            f"{body}\n\n"
            "You can also view and accept or decline this estimate online, here:\n"
            f"{estimate_link}"
        )

        msg = Message(
            subject=subject,
            recipients=[recipient_email],
            body=appended_body,
        )

        with open(filepath, "rb") as f:
            msg.attach(filename, "application/pdf", f.read())

        current_app.extensions["mail"].send(msg)

        now = datetime.now()
        status = str(estimate.get("status") or "").strip().lower()
        update_data = {
            "date_sent": now.strftime("%m/%d/%Y"),
            "time_sent": now.strftime("%H:%M:%S"),
        }
        if status not in {"accepted", "declined"}:
            update_data["status"] = "Sent"

        db.estimates.update_one({"_id": ObjectId(estimateId)}, {"$set": update_data})
        current_app.logger.info("Estimate email sent: estimate_id=%s to=%r by employee_id=%s", estimateId, recipient_email, session.get("employee_id"))
        return jsonify({"success": True}), 200
    except Exception as e:
        current_app.logger.error("Estimate email send failed: estimate_id=%s error=%s", estimateId, e)
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/estimates/<estimateId>/accept", methods=["POST"])
def accept_estimate(estimateId):
    db = ensure_connection_or_500()
    estimate = db.estimates.find_one({"_id": object_id_or_404(estimateId)})
    if not estimate:
        return redirect(url_for("home"))

    token_value = str(request.form.get("access_token") or request.args.get("token") or "").strip()
    is_staff_view = _is_authenticated_employee()
    if not is_staff_view and not _verify_estimate_access_token(estimate, token_value):
        return redirect(url_for("auth.login"))

    current_status = str(estimate.get("status") or "").strip().lower()
    if current_status in {"accepted", "declined"}:
        return redirect(_build_estimate_view_url(estimateId, access_token=token_value if not is_staff_view else ""))

    update_data = {}
    if not is_staff_view:
        signature_data_url = str(request.form.get("signature_data_url") or "").strip()
        if not signature_data_url.startswith("data:image/"):
            return redirect(_build_estimate_view_url(estimateId, access_token=token_value))

        update_data.update(
            {
                "accepted_signature_data_url": signature_data_url,
                "accepted_signature_ip": _extract_client_ip(),
                "accepted_signature_user_agent": str(request.headers.get("User-Agent") or "").strip(),
                "accepted_signature_captured_at": datetime.utcnow(),
                "accepted_signature_source": "public_estimate_link",
            }
        )

    now = datetime.now()
    update_data.update(
        {
            "status": "Accepted",
            "date_accepted": now.strftime("%m/%d/%Y"),
            "time_accepted": now.strftime("%H:%M:%S"),
        }
    )
    db.estimates.update_one(
        {"_id": ObjectId(estimateId)},
        {"$set": update_data},
    )

    try:
        created_job_id = _create_job_from_accepted_estimate(db, estimateId)
        if created_job_id:
            current_app.logger.info("Job auto-created from estimate acceptance: estimate_id=%s job_id=%s", estimateId, created_job_id)
    except Exception as exc:
        current_app.logger.error("Job auto-create failed for accepted estimate: estimate_id=%s error=%s", estimateId, exc)

    return redirect(_build_estimate_view_url(estimateId, access_token=token_value if not is_staff_view else ""))


@bp.route("/estimates/<estimateId>/decline", methods=["POST"])
def decline_estimate(estimateId):
    db = ensure_connection_or_500()
    estimate = db.estimates.find_one({"_id": object_id_or_404(estimateId)})
    if not estimate:
        return redirect(url_for("home"))

    token_value = str(request.form.get("access_token") or request.args.get("token") or "").strip()
    is_staff_view = _is_authenticated_employee()
    if not is_staff_view and not _verify_estimate_access_token(estimate, token_value):
        return redirect(url_for("auth.login"))

    current_status = str(estimate.get("status") or "").strip().lower()
    if current_status in {"accepted", "declined"}:
        return redirect(_build_estimate_view_url(estimateId, access_token=token_value if not is_staff_view else ""))

    now = datetime.now()
    db.estimates.update_one(
        {"_id": ObjectId(estimateId)},
        {
            "$set": {
                "status": "Declined",
                "date_declined": now.strftime("%m/%d/%Y"),
                "time_declined": now.strftime("%H:%M:%S"),
            }
        },
    )
    return redirect(_build_estimate_view_url(estimateId, access_token=token_value if not is_staff_view else ""))


@bp.route("/estimates/<estimateId>/delete", methods=["POST"])
def delete_estimate(estimateId):
    db = ensure_connection_or_500()
    estimate = db.estimates.find_one({"_id": object_id_or_404(estimateId)})
    redirect_target = url_for("home")
    if estimate:
        customer_id = str(estimate.get("customer_id") or "").strip()
        if customer_id:
            redirect_target = url_for("customers.view_customer", customerId=customer_id)
        estimate_file_path = resolve_estimate_file_path(estimate)
        if estimate_file_path:
            remove_estimate_pdf_file(estimate_file_path)

    db.estimates.delete_one({"_id": ObjectId(estimateId)})
    current_app.logger.info("Estimate deleted: id=%s by employee_id=%s", estimateId, session.get("employee_id"))
    return redirect(redirect_target)


@bp.route("/jobs/<jobId>/invoices/<invoiceRef>")
def view_invoice(jobId, invoiceRef):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    job_doc = serialize_doc(job)
    invoice = _find_invoice_entry(job_doc, invoice_ref=invoiceRef)
    if not invoice:
        return redirect(url_for("jobs.view_job", jobId=jobId))

    token_value = str(request.args.get("token") or "").strip()
    payment_state = str(request.args.get("payment") or "").strip().lower()
    returned_session_id = str(request.args.get("session_id") or "").strip()
    is_staff_view = _is_authenticated_employee()
    has_customer_token = _verify_invoice_access_token(invoice, token_value)
    session_access_key = f"invoice_access_{jobId}_{invoiceRef}"
    has_customer_session_access = bool(session.get(session_access_key))
    if not is_staff_view and not (has_customer_token or has_customer_session_access):
        return redirect(url_for("auth.login"))

    # Persist short-lived access for the specific invoice view so follow-up actions
    # (like starting checkout) can succeed even if query token forwarding is brittle.
    if not is_staff_view and has_customer_token:
        session[session_access_key] = True

    invoice_status = str((invoice or {}).get("payment_status") or "").strip().lower()
    job_status = str((job_doc or {}).get("status") or "").strip().lower()
    invoice_is_paid = invoice_status == "paid" or job_status == "paid"

    # Fallback: if webhook is delayed/missed, finalize payment from returned Checkout Session.
    if (
        payment_state == "success"
        and returned_session_id
        and "CHECKOUT_SESSION_ID" not in returned_session_id
        and not invoice_is_paid
        and _configure_stripe_client()
    ):
        try:
            checkout_session = stripe.checkout.Session.retrieve(returned_session_id)
            finalized = process_stripe_checkout_completed(db, checkout_session)
            if not finalized:
                current_app.logger.warning(
                    "Invoice success-return fallback could not finalize payment: job_id=%s invoice_ref=%s session_id=%s",
                    jobId,
                    invoiceRef,
                    returned_session_id,
                )

            refreshed_job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
            if refreshed_job:
                job_doc = serialize_doc(refreshed_job)
                refreshed_invoice = _find_invoice_entry(job_doc, invoice_ref=invoiceRef)
                if refreshed_invoice:
                    invoice = refreshed_invoice
        except Exception as exc:
            current_app.logger.warning(
                "Invoice success-return fallback failed: job_id=%s invoice_ref=%s session_id=%s error=%s",
                jobId,
                invoiceRef,
                returned_session_id,
                exc,
            )

    invoice_email_template = ""
    if is_staff_view:
        employee_id = session.get("employee_id")
        if employee_id and ObjectId.is_valid(employee_id):
            employee = db.employees.find_one({"_id": ObjectId(employee_id)})
            if employee:
                business_ref = employee.get("business")
                business_oid = None
                if isinstance(business_ref, ObjectId):
                    business_oid = business_ref
                elif isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
                    business_oid = ObjectId(business_ref)
                if business_oid:
                    business = db.businesses.find_one({"_id": business_oid})
                    if business:
                        invoice_email_template = business.get("invoice_email_template", "")

    customer = {}
    customer_id = job.get("customer_id")
    if customer_id:
        customer_doc = db.customers.find_one(build_reference_filter("_id", customer_id))
        if customer_doc:
            customer = serialize_doc(customer_doc)

    completed_dt = _parse_mmddyyyy_date(str(job_doc.get("dateCompleted") or "")[:10])
    payment_due_days = _normalize_payment_due_days(job_doc.get("payment_due_days"), 30)
    due_date = ""
    if completed_dt:
        due_date = (completed_dt + timedelta(days=payment_due_days)).strftime("%m/%d/%Y")

    stripe_connect_ready = False
    stripe_connect_reason = ""
    business_id = str(job_doc.get("business_id") or "").strip()
    if not business_id:
        stripe_connect_reason = "Missing business context on this invoice."
    else:
        business = db.businesses.find_one(build_reference_filter("_id", business_id)) or {}
        stripe_account_id = str((business or {}).get("stripe_account_id") or "").strip()
        charges_enabled = bool((business or {}).get("stripe_charges_enabled"))
        payouts_enabled = bool((business or {}).get("stripe_payouts_enabled"))
        stripe_connect_ready = bool(stripe_account_id and charges_enabled and payouts_enabled)
        if not stripe_connect_ready:
            stripe_connect_reason = "Card payments are unavailable until this business connects Stripe and enables charges/payouts."

    pricing_summary = _build_invoice_pricing_summary(job_doc)

    return render_template(
        "invoices/view_invoice.html",
        jobId=jobId,
        invoiceRef=invoiceRef,
        invoice=invoice,
        job=job_doc,
        customer=customer,
        pricing_summary=pricing_summary,
        payment_due_days=payment_due_days,
        due_date=due_date,
        is_staff_view=is_staff_view,
        access_token=token_value,
        invoice_email_template=invoice_email_template,
        payment_state=payment_state,
        stripe_publishable_key=_resolve_stripe_publishable_key(),
        stripe_connect_ready=stripe_connect_ready,
        stripe_connect_reason=stripe_connect_reason,
    )


@bp.route("/jobs/<jobId>/invoices/<invoiceRef>/stripe-checkout", methods=["GET", "POST"])
def create_invoice_checkout_session(jobId, invoiceRef):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404

    job_doc = serialize_doc(job)
    invoice = _find_invoice_entry(job_doc, invoice_ref=invoiceRef)
    if not invoice:
        return jsonify({"success": False, "error": "Invoice not found"}), 404

    request_data = request.get_json(silent=True) or {}
    token_value = str(request_data.get("access_token") or request.form.get("access_token") or request.args.get("token") or "").strip()
    is_staff_view = _is_authenticated_employee()
    session_access_key = f"invoice_access_{jobId}_{invoiceRef}"
    has_customer_session_access = bool(session.get(session_access_key))
    if not is_staff_view and not (_verify_invoice_access_token(invoice, token_value) or has_customer_session_access):
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    secret_key = _configure_stripe_client()
    if not secret_key:
        return jsonify({"success": False, "error": "Stripe is not configured"}), 500

    amount_total = int(round(float(job_doc.get("total_amount") or 0.0) * 100))
    if amount_total <= 0:
        return jsonify({"success": False, "error": "Invoice total must be greater than zero"}), 400

    business_id = str(job_doc.get("business_id") or "").strip()
    if not business_id:
        return jsonify({"success": False, "error": "Missing business context for this invoice"}), 400

    business = db.businesses.find_one(build_reference_filter("_id", business_id)) or {}
    stripe_account_id = str((business or {}).get("stripe_account_id") or "").strip()
    charges_enabled = bool((business or {}).get("stripe_charges_enabled"))
    payouts_enabled = bool((business or {}).get("stripe_payouts_enabled"))
    if not stripe_account_id or not charges_enabled or not payouts_enabled:
        return jsonify({"success": False, "error": "Business Stripe account is not connected for payments"}), 400

    platform_fee_percent = _resolve_platform_fee_percent(0.0)
    application_fee_amount = int(round(amount_total * (platform_fee_percent / 100.0))) if platform_fee_percent > 0 else 0

    customer_email = str((request_data.get("customer_email") or "")).strip() or str((job_doc.get("email") or "")).strip()
    if not customer_email:
        customer_email = str((db.customers.find_one(build_reference_filter("_id", job.get("customer_id")), {"email": 1}) or {}).get("email") or "").strip()

    success_url_base = url_for(
        "jobs.view_invoice",
        jobId=jobId,
        invoiceRef=invoiceRef,
        token=token_value if not is_staff_view else None,
        payment="success",
        _external=True,
    )
    success_url = f"{success_url_base}&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = url_for(
        "jobs.view_invoice",
        jobId=jobId,
        invoiceRef=invoiceRef,
        token=token_value if not is_staff_view else None,
        payment="canceled",
        _external=True,
    )

    checkout_session = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        customer_email=customer_email or None,
        client_reference_id=f"{jobId}:{str(invoice.get('invoice_id') or invoiceRef)}",
        line_items=[
            {
                "price_data": {
                    "currency": str(os.getenv("STRIPE_CURRENCY") or "usd").strip() or "usd",
                    "product_data": {
                        "name": _build_invoice_payment_label(job_doc, invoice),
                    },
                    "unit_amount": amount_total,
                },
                "quantity": 1,
            }
        ],
        metadata={
            "job_id": jobId,
            "invoice_ref": str(invoice.get("invoice_id") or invoice.get("invoice_number") or invoiceRef),
            "business_id": business_id,
        },
        payment_intent_data={
            "application_fee_amount": application_fee_amount,
            "transfer_data": {"destination": stripe_account_id},
            "metadata": {
                "job_id": jobId,
                "invoice_ref": str(invoice.get("invoice_id") or invoice.get("invoice_number") or invoiceRef),
                "business_id": business_id,
            },
        },
        success_url=success_url,
        cancel_url=cancel_url,
    )

    checkout_session_id = str(getattr(checkout_session, "id", "") or checkout_session.get("id") or "").strip()
    if checkout_session_id:
        updated_invoices = []
        normalized_invoice_ref = str(invoiceRef or "").strip()
        for entry in list(job_doc.get("invoices") or []):
            if not isinstance(entry, dict):
                updated_invoices.append(entry)
                continue
            entry_invoice_id = str(entry.get("invoice_id") or "").strip()
            entry_invoice_number = str(entry.get("invoice_number") or "").strip()
            updated_entry = dict(entry)
            if normalized_invoice_ref in {entry_invoice_id, entry_invoice_number}:
                updated_entry["stripe_checkout_session_id"] = checkout_session_id
            updated_invoices.append(updated_entry)

        db.jobs.update_one(
            {"_id": ObjectId(jobId)},
            {"$set": {"invoices": updated_invoices, "updated_at": datetime.utcnow()}},
        )

    return jsonify({"success": True, "checkout_url": checkout_session.url}), 200


@bp.route("/jobs/<jobId>")
def view_job(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    job_series = None
    job_series_id = job.get("series_id")
    if job_series_id:
        job_series = db.recurring_job_series.find_one({"_id": coerce_object_id(job_series_id)})

    quote_email_template = ""
    invoice_email_template = ""

    employee_id = session.get("employee_id")
    if employee_id and ObjectId.is_valid(employee_id):
        employee = db.employees.find_one({"_id": ObjectId(employee_id)})
        if employee:
            business_ref = employee.get("business")
            business_oid = None
            if isinstance(business_ref, ObjectId):
                business_oid = business_ref
            elif isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
                business_oid = ObjectId(business_ref)

            if business_oid:
                business = db.businesses.find_one({"_id": business_oid})
                if business:
                    quote_email_template = business.get("quote_email_template", "")
                    invoice_email_template = business.get("invoice_email_template", "")

    customer = {}
    customer_id = job.get("customer_id")
    if customer_id:
        customer_doc = db.customers.find_one(build_reference_filter("_id", customer_id))
        if customer_doc:
            customer = serialize_doc(customer_doc)

    job_doc = serialize_doc(job)
    job_doc["internal_notes"] = _build_internal_notes_for_view(db, job)

    # Detect if the viewing employee has a *different* active job (En Route / Started).
    # Used to disable transition buttons so only one job can be active at a time.
    employee_has_other_active_job = False
    current_employee_name = str(session.get("employee_name") or "").strip()
    normalized_viewer = " ".join(current_employee_name.lower().split())
    if normalized_viewer:
        _active_status_re = {"en route", "started"}
        other_active = db.jobs.find_one(
            {
                "_id": {"$ne": object_id_or_404(jobId)},
                "status": {"$in": ["En Route", "Started"]},
                "assigned_employee": {"$regex": f"^{current_employee_name}$", "$options": "i"},
            },
            {"_id": 1},
        )
        employee_has_other_active_job = other_active is not None

    return render_template(
        "jobs/view_job.html",
        jobId=jobId,
        job=job_doc,
        job_series=serialize_doc(job_series) if job_series else None,
        customer=customer,
        quote_email_template=quote_email_template,
        invoice_email_template=invoice_email_template,
        employee_has_other_active_job=employee_has_other_active_job,
    )


@bp.route("/jobs/<jobId>/internal-notes", methods=["POST"])
def add_internal_note(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    note_text = str(request.form.get("internal_note") or "").strip()
    if not note_text:
        return redirect(url_for("jobs.view_job", jobId=jobId))

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {
            "$push": {
                "internal_notes": {
                    "note_id": str(ObjectId()),
                    "text": note_text,
                    "date_written": datetime.now().strftime("%m/%d/%Y %H:%M:%S"),
                    "employee_id": str(session.get("employee_id") or "").strip(),
                }
            }
        },
    )
    next_url = request.form.get("next") or url_for("jobs.view_job", jobId=jobId)
    return redirect(next_url)


@bp.route("/jobs/<jobId>/internal-notes/<noteId>/delete", methods=["POST"])
def delete_internal_note(jobId, noteId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {"$pull": {"internal_notes": {"note_id": str(noteId or "").strip()}}},
    )
    return redirect(url_for("jobs.view_job", jobId=jobId))


@bp.route("/jobs/<jobId>/series/pause", methods=["POST"])
def pause_recurring_series(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    series_id = coerce_object_id(job.get("series_id"))
    if not series_id:
        return redirect(url_for("jobs.view_job", jobId=jobId))

    db.recurring_job_series.update_one(
        {"_id": series_id},
        {"$set": {"status": "Paused"}},
    )
    current_app.logger.info("Recurring series paused: series_id=%s by employee_id=%s", str(series_id), session.get("employee_id"))
    return redirect(url_for("jobs.view_job", jobId=jobId))


@bp.route("/jobs/<jobId>/series/resume", methods=["POST"])
def resume_recurring_series(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    series_id = coerce_object_id(job.get("series_id"))
    if not series_id:
        return redirect(url_for("jobs.view_job", jobId=jobId))

    series_doc = db.recurring_job_series.find_one({"_id": series_id})
    if not series_doc:
        return redirect(url_for("jobs.view_job", jobId=jobId))

    db.recurring_job_series.update_one(
        {"_id": series_id},
        {"$set": {"status": "Active"}},
    )

    pending_occurrence = db.jobs.find_one(
        {
            "series_id": series_id,
            "status": {"$in": ["Pending", "Scheduled"]},
        },
        sort=[("occurrence_index", 1), ("_id", 1)],
    )
    if pending_occurrence:
        db.recurring_job_series.update_one(
            {"_id": series_id},
            {"$set": {"next_occurrence_date": str(pending_occurrence.get("scheduled_date") or "").strip()}},
        )
    else:
        latest_occurrence = db.jobs.find_one(
            {"series_id": series_id},
            sort=[("occurrence_index", -1), ("_id", -1)],
        )
        if latest_occurrence:
            latest_index = int(latest_occurrence.get("occurrence_index") or 0)
            next_date = _advance_recurring_date(
                _parse_mmddyyyy_date(latest_occurrence.get("scheduled_date")),
                series_doc.get("frequency"),
            )
            next_text = _format_mmddyyyy_date(next_date)
            if next_text:
                _create_occurrence_from_series(db, series_doc, next_text, latest_index + 1)

    current_app.logger.info("Recurring series resumed: series_id=%s by employee_id=%s", str(series_id), session.get("employee_id"))
    return redirect(url_for("jobs.view_job", jobId=jobId))


@bp.route("/jobs/<jobId>/series/cancel", methods=["POST"])
def cancel_recurring_series(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    series_id = coerce_object_id(job.get("series_id"))
    if not series_id:
        return redirect(url_for("jobs.view_job", jobId=jobId))

    db.recurring_job_series.update_one(
        {"_id": series_id},
        {"$set": {"status": "Cancelled", "next_occurrence_date": ""}},
    )

    db.jobs.delete_many(
        {
            "series_id": series_id,
            "_id": {"$ne": job["_id"]},
            "status": {"$in": ["Pending", "Scheduled"]},
        }
    )

    current_app.logger.info("Recurring series cancelled: series_id=%s by employee_id=%s", str(series_id), session.get("employee_id"))
    return redirect(url_for("jobs.view_job", jobId=jobId))


@bp.route("/jobs/<jobId>/start", methods=["POST"])
def start_job(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    current_status = str(job.get("status") or "").strip().lower()
    if current_status != "en route":
        current_app.logger.warning("Blocked invalid job start: job_id=%s status=%s", jobId, current_status)
        return redirect(url_for("jobs.view_job", jobId=jobId))

    current_timestamp = datetime.now().strftime("%m/%d/%Y %H:%M:%S")
    current_timestamp_utc = datetime.utcnow()

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {"$set": {"status": "Started", "dateStarted": current_timestamp, "started_at": current_timestamp_utc, "updated_at": current_timestamp_utc}},
    )

    next_url = request.form.get("next") or url_for("jobs.view_job", jobId=jobId)
    return redirect(next_url)


@bp.route("/jobs/<jobId>/en-route", methods=["POST"])
def en_route_job(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    current_status = str(job.get("status") or "").strip().lower()
    has_schedule = bool(str(job.get("scheduled_date") or "").strip()) and bool(str(job.get("scheduled_time") or "").strip())
    if current_status != "scheduled" or not has_schedule:
        current_app.logger.warning("Blocked invalid en-route transition: job_id=%s status=%s has_schedule=%s", jobId, current_status, has_schedule)
        return redirect(url_for("jobs.view_job", jobId=jobId))

    current_timestamp_utc = datetime.utcnow()

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {"$set": {"status": "En Route", "en_route_at": current_timestamp_utc, "updated_at": current_timestamp_utc}},
    )

    next_url = request.form.get("next") or url_for("jobs.view_job", jobId=jobId)
    return redirect(next_url)


@bp.route("/jobs/<jobId>/complete", methods=["POST"])
def complete_job(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    customer = {}
    customer_id = job.get("customer_id")
    customer_oid = None
    customer_doc = None
    if customer_id:
        customer_doc = db.customers.find_one(build_reference_filter("_id", customer_id))
        if customer_doc:
            customer_oid = customer_doc.get("_id")
        if customer_doc:
            customer = serialize_doc(customer_doc)

    business = {}
    business_id = resolve_current_business_id(db)
    if business_id:
        business_doc = db.businesses.find_one(
            {"_id": business_id},
            {
                "company_name": 1,
                "business_name": 1,
                "address_line_1": 1,
                "address_line_2": 1,
                "city": 1,
                "state": 1,
                "zip_code": 1,
                "phone_number": 1,
                "fax_number": 1,
                "email": 1,
                "website": 1,
                "license_number": 1,
                "warranty_info": 1,
                "tax_parts": 1,
                "tax_parts_rate": 1,
                "tax_repair_labor": 1,
                "tax_repair_labor_rate": 1,
                "tax_materials": 1,
                "tax_materials_rate": 1,
                "tax_installation": 1,
                "tax_installation_rate": 1,
                "tax_fabrication": 1,
                "tax_fabrication_rate": 1,
            },
        )
        if business_doc:
            business = serialize_doc(business_doc)

    business_logo_path = resolve_current_business_logo_path(db)
    invoice_path = generate_invoice(jobId, job, customer, business_logo_path=business_logo_path, business=business)
    filename = os.path.basename(invoice_path)

    current_timestamp = datetime.now().strftime("%m/%d/%Y %H:%M:%S")
    current_timestamp_utc = datetime.utcnow()
    time_spent_str = ""

    date_started = job.get("dateStarted")
    if date_started:
        try:
            started_dt = datetime.strptime(date_started, "%m/%d/%Y %H:%M:%S")
            completed_dt = datetime.strptime(current_timestamp, "%m/%d/%Y %H:%M:%S")
            time_diff = completed_dt - started_dt
            total_seconds = int(time_diff.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            if hours > 0 and minutes > 0:
                time_spent_str = f"{hours} hour{'s' if hours != 1 else ''} {minutes} minute{'s' if minutes != 1 else ''}"
            elif hours > 0:
                time_spent_str = f"{hours} hour{'s' if hours != 1 else ''}"
            elif minutes > 0:
                time_spent_str = f"{minutes} minute{'s' if minutes != 1 else ''}"
            else:
                time_spent_str = "Less than 1 minute"
        except ValueError:
            time_spent_str = ""

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {
            "$set": {
                "status": "Completed",
                "dateCompleted": current_timestamp,
                "completed_at": current_timestamp_utc,
                "updated_at": current_timestamp_utc,
                "timeSpent": time_spent_str,
            },
            "$push": {
                "invoices": {
                    "invoice_id": str(ObjectId()),
                    "invoice_number": f"INV-{jobId[:8].upper()}",
                    "file_path": url_for("download_invoice", filename=filename),
                }
            },
        },
    )

    try:
        _create_maintenance_records(db, jobId, job, business_id)
    except Exception as _maint_exc:
        current_app.logger.error("Maintenance records failed: job_id=%s error=%s", jobId, _maint_exc)

    if str(job.get("job_kind") or "").strip() == "recurring_occurrence" and job.get("series_id"):
        series_doc = db.recurring_job_series.find_one({"_id": coerce_object_id(job.get("series_id"))})
        occurrence_index = int(job.get("occurrence_index") or 0)
        next_occurrence_date = _advance_recurring_date(_parse_mmddyyyy_date(job.get("scheduled_date")), (series_doc or {}).get("frequency"))
        next_occurrence_text = _format_mmddyyyy_date(next_occurrence_date)
        if series_doc and next_occurrence_text:
            _create_occurrence_from_series(db, series_doc, next_occurrence_text, occurrence_index + 1)

    next_url = request.form.get("next", "").strip()

    current_app.logger.info("Job completed: id=%s invoice=%s by employee_id=%s", jobId, filename, session.get("employee_id"))

    if customer_oid and customer_doc:
        current_balance = float(customer_doc.get("balance_due_amount", currency_to_float(customer_doc.get("balance_due", "$0.00"))))
        invoice_total = float(job.get("total_amount", 0.0))
        updated_balance_amount = float(current_balance + invoice_total)
        updated_balance = normalize_currency(str(updated_balance_amount))

        db.customers.update_one(
            {"_id": customer_oid},
            {
                "$set": {
                    "balance_due": updated_balance,
                    "balance_due_amount": updated_balance_amount,
                }
            },
        )

    return redirect(next_url if next_url else url_for("jobs.view_job", jobId=jobId))


@bp.route("/jobs/<jobId>/email-estimate", methods=["POST"])
def send_estimate_email(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404

    try:
        data = request.get_json() or {}
        recipient_email = data.get("recipient_email", "")
        subject = data.get("subject", "")
        body = data.get("body", "")
        estimate_file = str(data.get("estimate_file", "") or "").strip()
        email_type = str(data.get("email_type", "estimate") or "estimate").strip().lower()

        if not recipient_email or not subject or not body:
            return jsonify({"success": False, "error": "Missing required fields"}), 400

        resolved_invoice_entry = None
        if email_type == "invoice":
            job_doc = serialize_doc(job)
            resolved_invoice_entry = _find_invoice_entry(job_doc, file_path=estimate_file)

            if not resolved_invoice_entry:
                # Subject typically looks like "Invoice: INV-XXXXXXXX".
                subject_invoice_ref = str(subject or "").split(":", 1)[-1].strip()
                if subject_invoice_ref:
                    resolved_invoice_entry = _find_invoice_entry(job_doc, invoice_ref=subject_invoice_ref)

            if not resolved_invoice_entry:
                invoices = [entry for entry in (job_doc.get("invoices") or []) if isinstance(entry, dict)]
                if invoices:
                    resolved_invoice_entry = invoices[-1]

            if not estimate_file and resolved_invoice_entry:
                estimate_file = str(resolved_invoice_entry.get("file_path") or "").strip()

        filepath = estimate_pdf_absolute_path_from_url(estimate_file)
        if not filepath or not os.path.exists(filepath):
            if email_type == "invoice":
                return jsonify({"success": False, "error": "Invoice file not found"}), 404
            return jsonify({"success": False, "error": "Estimate file not found"}), 404

        filename = os.path.basename(filepath)

        appended_body = body
        if email_type == "invoice":
            invoice_entry = resolved_invoice_entry
            if not invoice_entry:
                job_doc = serialize_doc(job)
                invoice_entry = _find_invoice_entry(job_doc, file_path=estimate_file)
            invoice_ref = ""
            if invoice_entry:
                invoice_ref = str(invoice_entry.get("invoice_id") or invoice_entry.get("invoice_number") or "").strip()
            if invoice_ref:
                access_token = _issue_invoice_access_token(db, jobId, invoice_ref, recipient_email)
                if access_token:
                    invoice_link = _build_invoice_view_url(jobId, invoice_ref, access_token=access_token, external=True)
                    appended_body = (
                        f"{body}\n\n"
                        "You can also view this invoice online here:\n"
                        f"{invoice_link}"
                    )

        msg = Message(
            subject=subject,
            recipients=[recipient_email],
            body=appended_body,
        )

        with open(filepath, "rb") as f:
            msg.attach(filename, "application/pdf", f.read())

        current_app.extensions["mail"].send(msg)

        if email_type == "invoice":
            db.jobs.update_one(
                {"_id": ObjectId(jobId)},
                {"$set": {"date_invoice_sent": datetime.now().strftime("%m/%d/%Y %H:%M:%S")}},
            )

        current_app.logger.info("Estimate email sent: job_id=%s to=%r by employee_id=%s", jobId, recipient_email, session.get("employee_id"))
        return jsonify({"success": True}), 200

    except Exception as e:
        current_app.logger.error("Email send failed: job_id=%s error=%s", jobId, e)
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/jobs/<jobId>/update", methods=["GET", "POST"])
def update_job(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    existing_series = None
    if job.get("series_id"):
        existing_series = db.recurring_job_series.find_one({"_id": coerce_object_id(job.get("series_id"))})
    recurrence_locked = str(job.get("job_kind") or "").strip() == "recurring_occurrence"

    if request.method == "POST":
        business_id = resolve_current_business_id(db)
        default_payment_due_days = _resolve_default_payment_due_days(db)
        selected_property_id = request.form.get("job_property_id", "").strip()
        customer_for_property = {}
        job_customer_id = job.get("customer_id")
        if job_customer_id:
            customer_for_property = db.customers.find_one(build_reference_filter("_id", job_customer_id)) or {}
        if not selected_property_id:
            default_property = _resolve_default_property(customer_for_property)
            selected_property_id = str((default_property or {}).get("property_id") or "").strip()
        selected_property = _resolve_selected_property(customer_for_property, selected_property_id)
        selected_service_types = request.form.getlist("service_code[]") or request.form.getlist("service_type[]")
        entered_service_prices = request.form.getlist("service_price[]") or request.form.getlist("service_standard_price[]")
        entered_service_durations = request.form.getlist("service_hours[]") or request.form.getlist("service_estimated_hours[]") or request.form.getlist("service_duration[]")
        entered_service_emergency_calls = request.form.getlist("service_emergency_call[]")
        selected_part_names = request.form.getlist("part_code[]") or request.form.getlist("part_name[]")
        entered_part_prices = request.form.getlist("part_unit_cost[]") or request.form.getlist("part_price[]")
        selected_labor_descriptions = request.form.getlist("labor_description[]")
        entered_labor_hours = request.form.getlist("labor_hours[]")
        entered_labor_rates = request.form.getlist("labor_hourly_rate[]")
        selected_material_names = request.form.getlist("material_name[]")
        entered_material_quantities = request.form.getlist("material_quantity_used[]")
        entered_material_units = request.form.getlist("material_unit_of_measure[]")
        entered_material_prices = request.form.getlist("material_price[]")
        selected_equipment_names = request.form.getlist("equipment_name[]")
        entered_equipment_quantities = request.form.getlist("equipment_quantity_installed[]")
        entered_equipment_prices = request.form.getlist("equipment_price[]")
        selected_discount_names = request.form.getlist("discount_name[]")
        entered_discount_percentages = request.form.getlist("discount_percentage[]")
        entered_discount_amounts = request.form.getlist("discount_amount[]")
        service_query = {"business_id": business_id} if business_id else {"_id": None}
        part_query = {"business_id": business_id} if business_id else {"_id": None}
        labor_query = {"business_id": business_id} if business_id else {"_id": None}
        material_query = {"business_id": business_id} if business_id else {"_id": None}
        equipment_query = {"business_id": business_id} if business_id else {"_id": None}
        discount_query = {"business_id": business_id} if business_id else {"_id": None}
        service_docs = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
        part_docs = [serialize_doc(part) for part in db.parts.find(part_query).sort("part_name", 1)]
        labor_docs = [serialize_doc(labor) for labor in db.labors.find(labor_query).sort("labor_description", 1)]
        material_docs = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
        equipment_docs = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
        discount_docs = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
        service_catalog = build_service_catalog(service_docs)
        part_catalog = build_part_catalog(part_docs)
        labor_catalog = build_labor_catalog(labor_docs)
        material_catalog = build_material_catalog(material_docs)
        equipment_catalog = build_equipment_catalog(equipment_docs)
        discount_catalog = build_discount_catalog(discount_docs)
        services, services_total = build_job_services_from_form(
            selected_service_types,
            entered_service_prices,
            entered_service_durations,
            service_catalog,
            entered_service_emergency_calls,
        )
        parts, parts_total = build_job_parts_from_form(
            selected_part_names,
            entered_part_prices,
            part_catalog,
        )
        labors, labor_total = build_job_labors_from_form(
            selected_labor_descriptions,
            entered_labor_hours,
            entered_labor_rates,
            labor_catalog,
        )
        materials, materials_total = build_job_materials_from_form(
            selected_material_names,
            entered_material_quantities,
            entered_material_units,
            entered_material_prices,
            material_catalog,
        )
        equipments, equipment_total = build_job_equipments_from_form(
            selected_equipment_names,
            entered_equipment_quantities,
            entered_equipment_prices,
            equipment_catalog,
        )
        discounts, discounts_total = build_job_discounts_from_form(
            selected_discount_names,
            entered_discount_percentages,
            entered_discount_amounts,
            discount_catalog,
        )
        _apply_hvac_ids_to_components(services, request.form.getlist("service_hvac_system_ids[]"))
        _apply_hvac_ids_to_components(parts, request.form.getlist("part_hvac_system_ids[]"))
        _apply_hvac_ids_to_components(labors, request.form.getlist("labor_hvac_system_ids[]"))
        _apply_hvac_ids_to_components(materials, request.form.getlist("material_hvac_system_ids[]"))
        _apply_hvac_ids_to_components(equipments, request.form.getlist("equipment_hvac_system_ids[]"))
        total = services_total + parts_total + labor_total + materials_total + equipment_total - discounts_total

        primary_service = services[0]["type"] if services else "No services added."
        scheduled_date = format_date(request.form.get("job_date", ""))
        scheduled_time = request.form.get("job_time", "").strip()
        payment_due_days = _normalize_payment_due_days(
            request.form.get("payment_due_days", ""),
            default_payment_due_days,
        )
        payment_due_days_offset = payment_due_days
        existing_scheduled_date = str(job.get("scheduled_date") or "").strip()
        existing_scheduled_time = str(job.get("scheduled_time") or "").strip()
        existing_date_scheduled = str(job.get("dateScheduled") or "").strip()
        if scheduled_date and scheduled_time:
            if (
                scheduled_date != existing_scheduled_date
                or scheduled_time != existing_scheduled_time
                or not existing_date_scheduled
            ):
                date_scheduled = datetime.now().strftime("%m/%d/%Y")
            else:
                date_scheduled = existing_date_scheduled
        else:
            date_scheduled = ""
        job_status = resolve_job_status(
            scheduled_date,
            scheduled_time,
            services,
            parts,
            labors,
            materials,
            equipments,
            discounts,
            existing_status=job.get("status", ""),
        )

        assigned_employee = request.form.get("job_assigned_employee", "").strip()
        recurring_data = _parse_recurrence_request(
            request,
            scheduled_date,
            scheduled_time,
            existing_series=existing_series,
            lock_to_recurring=recurrence_locked,
        )

        update_data = {
            "job_type": primary_service,
            "services": services,
            "parts": parts,
            "labors": labors,
            "materials": materials,
            "equipments": equipments,
            "discounts": discounts,
            "property_id": selected_property_id if selected_property else "",
            "property_name": (selected_property or {}).get("property_name") or "",
            "status": job_status,
            "scheduled_date": scheduled_date,
            "scheduled_time": scheduled_time,
            "dateScheduled": date_scheduled,
            "address_line_1": request.form.get("job_address_line_1", "").strip(),
            "address_line_2": request.form.get("job_address_line_2", "").strip(),
            "city": request.form.get("job_city", "").strip(),
            "state": request.form.get("job_state", "").strip().upper(),
            "zip_code": request.form.get("job_zip_code", "").strip(),
            "assigned_employee": assigned_employee,
            "invoice_notes": request.form.get("invoice_notes", "").strip(),
            "payment_due_days": payment_due_days,
            "total_amount": float(total or 0.0),
        }

        if recurring_data.get("is_recurring"):
            if existing_series:
                next_occurrence_date = _advance_recurring_date(_parse_mmddyyyy_date(scheduled_date), recurring_data.get("frequency"))
                next_occurrence_text = _format_mmddyyyy_date(next_occurrence_date)
                if next_occurrence_text and not _series_allows_occurrence(existing_series, int(job.get("occurrence_index") or 0) + 1, next_occurrence_text):
                    next_occurrence_text = ""

                series_update = {
                    "customer_name": str(job.get("customer_name") or "").strip(),
                    "company": str(job.get("company") or "").strip(),
                    "property_id": selected_property_id if selected_property else "",
                    "property_name": (selected_property or {}).get("property_name") or "",
                    "job_type": primary_service,
                    "services": services,
                    "parts": parts,
                    "labors": labors,
                    "materials": materials,
                    "equipments": equipments,
                    "discounts": discounts,
                    "frequency": recurring_data.get("frequency"),
                    "anchor_time": scheduled_time,
                    "end_type": recurring_data.get("end_type") or "never",
                    "end_date": recurring_data.get("end_date") or "",
                    "max_occurrences": recurring_data.get("max_occurrences"),
                    "next_occurrence_date": next_occurrence_text,
                    "address_line_1": request.form.get("job_address_line_1", "").strip(),
                    "address_line_2": request.form.get("job_address_line_2", "").strip(),
                    "city": request.form.get("job_city", "").strip(),
                    "state": request.form.get("job_state", "").strip().upper(),
                    "zip_code": request.form.get("job_zip_code", "").strip(),
                    "assigned_employee": assigned_employee,
                    "invoice_notes": request.form.get("invoice_notes", "").strip(),
                    "payment_due_days_offset": payment_due_days_offset,
                    "total_amount": float(total or 0.0),
                }
                if int(job.get("occurrence_index") or 0) == 1:
                    series_update["anchor_date"] = scheduled_date
                db.recurring_job_series.update_one({"_id": existing_series["_id"]}, {"$set": series_update})
                update_data.update(
                    {
                        "job_kind": "recurring_occurrence",
                        "series_id": existing_series["_id"],
                        "occurrence_index": int(job.get("occurrence_index") or 1),
                        "recurrence_summary": recurring_data.get("summary") or _build_recurrence_summary(existing_series.get("frequency")),
                    }
                )
            else:
                customer_for_series = customer_for_property or {}
                series_doc = _build_recurring_series_document(
                    customer_for_series,
                    business_id,
                    selected_property,
                    selected_property_id,
                    primary_service,
                    services,
                    parts,
                    labors,
                    materials,
                    equipments,
                    discounts,
                    total,
                    assigned_employee,
                    recurring_data,
                    scheduled_date,
                    scheduled_time,
                    payment_due_days_offset,
                    request,
                )
                inserted_series = db.recurring_job_series.insert_one(series_doc)
                update_data.update(
                    {
                        "job_kind": "recurring_occurrence",
                        "series_id": inserted_series.inserted_id,
                        "occurrence_index": 1,
                        "recurrence_summary": recurring_data.get("summary"),
                    }
                )
                existing_series = dict(series_doc)
                existing_series["_id"] = inserted_series.inserted_id
        else:
            update_data.update(
                {
                    "job_kind": "one_time",
                    "series_id": None,
                    "occurrence_index": None,
                    "recurrence_summary": "",
                }
            )

        db.jobs.update_one(
            {"_id": ObjectId(jobId)},
            {"$set": update_data},
        )

        return redirect(url_for("jobs.view_job", jobId=jobId))

    customer = {}
    customer_id = job.get("customer_id")
    if customer_id:
        customer_doc = db.customers.find_one(build_reference_filter("_id", customer_id))
        if customer_doc:
            customer = serialize_doc(customer_doc)

    business_id = resolve_current_business_id(db)
    service_query = {"business_id": business_id} if business_id else {"_id": None}
    part_query = {"business_id": business_id} if business_id else {"_id": None}
    labor_query = {"business_id": business_id} if business_id else {"_id": None}
    material_query = {"business_id": business_id} if business_id else {"_id": None}
    equipment_query = {"business_id": business_id} if business_id else {"_id": None}
    discount_query = {"business_id": business_id} if business_id else {"_id": None}
    services = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
    parts = [serialize_doc(part) for part in db.parts.find(part_query).sort("part_name", 1)]
    labors = [serialize_doc(labor) for labor in db.labors.find(labor_query).sort("labor_description", 1)]
    materials = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
    equipments = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
    discounts = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
    employee_options = build_employee_options(db)
    services_catalog = build_service_catalog(services)
    parts_catalog = build_part_catalog(parts)
    labors_catalog = build_labor_catalog(labors)
    materials_catalog = build_material_catalog(materials)
    equipments_catalog = build_equipment_catalog(equipments)
    discounts_catalog = build_discount_catalog(discounts)
    parts_by_id = {p["_id"]: p["part_code"] for p in parts}
    materials_by_id = {m["_id"]: m["material_name"] for m in materials}
    default_payment_due_days = _resolve_default_payment_due_days(db)
    payment_due_days_value = _normalize_payment_due_days(job.get("payment_due_days"), default_payment_due_days)
    selected_property_id = str(job.get("property_id") or "").strip()
    if not selected_property_id:
        default_property = _resolve_default_property(customer)
        selected_property_id = str((default_property or {}).get("property_id") or "").strip()

    job_doc = serialize_doc(job)
    initial_hvac_systems = _query_hvac_systems_for_property(db, reference_value(job.get("customer_id")), selected_property_id)
    job_services_hvac = [s.get("hvac_system_ids") or [] for s in (job_doc.get("services") or [])]
    job_parts_hvac = [p.get("hvac_system_ids") or [] for p in (job_doc.get("parts") or [])]
    job_labors_hvac = [l.get("hvac_system_ids") or [] for l in (job_doc.get("labors") or [])]
    job_materials_hvac = [m.get("hvac_system_ids") or [] for m in (job_doc.get("materials") or [])]
    job_equipments_hvac = [e.get("hvac_system_ids") or [] for e in (job_doc.get("equipments") or [])]

    return render_template(
        "jobs/update_job.html",
        jobId=jobId,
        job=job_doc,
        recurrence_state=_build_recurrence_form_state(job=job, series=existing_series),
        recurrence_locked=recurrence_locked,
        customer=customer,
        customer_properties=_get_customer_properties(customer),
        selected_property_id=selected_property_id,
        initial_hvac_systems=initial_hvac_systems,
        job_services_hvac=job_services_hvac,
        job_parts_hvac=job_parts_hvac,
        job_labors_hvac=job_labors_hvac,
        job_materials_hvac=job_materials_hvac,
        job_equipments_hvac=job_equipments_hvac,
        services=services,
        parts=parts,
        labors=labors,
        materials=materials,
        equipments=equipments,
        discounts=discounts,
        employee_options=employee_options,
        services_catalog=services_catalog,
        parts_catalog=parts_catalog,
        labors_catalog=labors_catalog,
        materials_catalog=materials_catalog,
        equipments_catalog=equipments_catalog,
        discounts_catalog=discounts_catalog,
        parts_by_id=parts_by_id,
        materials_by_id=materials_by_id,
        payment_due_days_value=payment_due_days_value,
        recurring_frequency_options=RECURRING_FREQUENCY_OPTIONS,
        recurring_end_type_options=RECURRING_END_TYPE_OPTIONS,
    )


@bp.route("/jobs/<jobId>/delete", methods=["POST"])
def delete_job(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    delete_scope = str(request.form.get("delete_scope") or "occurrence_only").strip().lower()
    series_id = coerce_object_id(job.get("series_id"))

    if series_id and delete_scope == "series_future":
        db.recurring_job_series.update_one(
            {"_id": series_id},
            {"$set": {"status": "Cancelled", "next_occurrence_date": ""}},
        )
        db.jobs.delete_many(
            {
                "series_id": series_id,
                "_id": {"$ne": job["_id"]},
                "status": {"$in": ["Pending", "Scheduled"]},
            }
        )

    db.estimates.delete_many({"job_id": jobId})
    db.jobs.delete_one({"_id": job["_id"]})
    current_app.logger.info("Job deleted: id=%s by employee_id=%s", jobId, session.get("employee_id"))
    return redirect(url_for("jobs.jobs"))
