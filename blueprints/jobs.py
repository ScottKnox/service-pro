import calendar
from datetime import datetime, timedelta
import json
import os

from bson import ObjectId
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for
from flask_mail import Message

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


def _build_recurring_series_document(customer, business_id, selected_property, selected_property_id, primary_service, services, parts, labors, materials, equipments, discounts, total, assigned_employee, recurring_data, scheduled_date, scheduled_time, request_obj):
    series_anchor_date = scheduled_date
    anchor_date_dt = _parse_mmddyyyy_date(series_anchor_date)
    next_occurrence_date = _advance_recurring_date(anchor_date_dt, recurring_data.get("frequency"))
    next_occurrence_text = _format_mmddyyyy_date(next_occurrence_date)

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
        "total": f"${total:.2f}" if total else "$0.00",
        "total_amount": float(total or 0.0),
        "invoice_notes": request_obj.form.get("invoice_notes", "").strip(),
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
        "total": str(series_doc.get("total") or "$0.00").strip() or "$0.00",
        "total_amount": float(series_doc.get("total_amount") or 0.0),
        "invoice_notes": str(series_doc.get("invoice_notes") or "").strip(),
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
    estimate_notes = str(serialized.get("estimate_notes") or "").strip()
    serialized["notes"] = [{"text": estimate_notes}] if estimate_notes else []
    return serialized


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
    return build_csv_export_response(jobs_rows, "jobs_export.csv")


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
    return build_csv_export_response(estimates_rows, "estimates_export.csv")


@bp.route("/customers/<customerId>/jobs/create", methods=["GET", "POST"])
def create_job(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    if request.method == "POST":
        business_id = resolve_current_business_id(db)
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

        primary_service = services[0]["type"] if services else "No services added."
        scheduled_date = format_date(request.form.get("job_date", ""))
        scheduled_time = request.form.get("job_time", "").strip()
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
            "total": f"${total:.2f}" if total else "$0.00",
            "total_amount": float(total or 0.0),
            "invoice_notes": invoice_notes,
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
    default_property = _resolve_default_property(customer)
    selected_property_id = str((default_property or {}).get("property_id") or "").strip()
    initial_address_line_1 = (default_property or {}).get("address_line_1") or customer.get("address_line_1", "")
    initial_address_line_2 = (default_property or {}).get("address_line_2") or customer.get("address_line_2", "")
    initial_city = (default_property or {}).get("city") or customer.get("city", "")
    initial_state = (default_property or {}).get("state") or customer.get("state", "")
    initial_zip_code = (default_property or {}).get("zip_code") or customer.get("zip_code", "")

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
            "total": f"${total:.2f}" if total else "$0.00",
            "total_amount": float(total or 0.0),
            "estimate_notes": estimate_notes,
            "file_path": [],
            "latest_file_path": "",
            "created_at": datetime.utcnow(),
        }

        inserted = db.estimates.insert_one(new_estimate)
        estimate_id = str(inserted.inserted_id)

        try:
            business_logo_path = resolve_current_business_logo_path(db)
            estimate_pdf_path = generate_estimate(
                estimate_id,
                serialize_estimate_for_pdf(new_estimate),
                serialize_doc(customer),
                business_logo_path=business_logo_path,
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
    )


@bp.route("/estimates/<estimateId>")
def view_estimate(estimateId):
    db = ensure_connection_or_500()
    estimate = db.estimates.find_one({"_id": object_id_or_404(estimateId)})
    if not estimate:
        return redirect(url_for("home"))

    quote_email_template = ""
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

    customer = {}
    customer_id = estimate.get("customer_id")
    if customer_id:
        customer_doc = db.customers.find_one(build_reference_filter("_id", customer_id))
        if customer_doc:
            customer = serialize_doc(customer_doc)

    estimate_doc = serialize_doc(estimate)
    estimate_doc["latest_file_path"] = resolve_estimate_file_path(estimate_doc)

    return render_template(
        "estimates/view_estimate.html",
        estimateId=estimateId,
        estimate=estimate_doc,
        customer=customer,
        quote_email_template=quote_email_template,
    )


@bp.route("/estimates/<estimateId>/update", methods=["GET", "POST"])
def update_estimate(estimateId):
    db = ensure_connection_or_500()
    estimate = db.estimates.find_one({"_id": object_id_or_404(estimateId)})
    if not estimate:
        return redirect(url_for("home"))

    if request.method == "POST":
        business_id = resolve_current_business_id(db)
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
            "estimate_notes": estimate_notes,
            "total": f"${total:.2f}" if total else "$0.00",
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
        previous_file_path = resolve_estimate_file_path(estimate)
        estimate_pdf_path = generate_estimate(
            estimateId,
            serialize_estimate_for_pdf(estimate_for_pdf),
            customer,
            business_logo_path=business_logo_path,
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
    selected_property_id = str(estimate_doc.get("property_id") or "").strip()
    if not selected_property_id:
        default_property = _resolve_default_property(customer)
        selected_property_id = str((default_property or {}).get("property_id") or "").strip()

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

        msg = Message(
            subject=subject,
            recipients=[recipient_email],
            body=body,
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

    current_status = str(estimate.get("status") or "").strip().lower()
    if current_status in {"accepted", "declined"}:
        return redirect(url_for("jobs.view_estimate", estimateId=estimateId))

    now = datetime.now()
    db.estimates.update_one(
        {"_id": ObjectId(estimateId)},
        {
            "$set": {
                "status": "Accepted",
                "date_accepted": now.strftime("%m/%d/%Y"),
                "time_accepted": now.strftime("%H:%M:%S"),
            }
        },
    )
    return redirect(url_for("jobs.view_estimate", estimateId=estimateId))


@bp.route("/estimates/<estimateId>/decline", methods=["POST"])
def decline_estimate(estimateId):
    db = ensure_connection_or_500()
    estimate = db.estimates.find_one({"_id": object_id_or_404(estimateId)})
    if not estimate:
        return redirect(url_for("home"))

    current_status = str(estimate.get("status") or "").strip().lower()
    if current_status in {"accepted", "declined"}:
        return redirect(url_for("jobs.view_estimate", estimateId=estimateId))

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
    return redirect(url_for("jobs.view_estimate", estimateId=estimateId))


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

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {"$set": {"status": "Started", "dateStarted": current_timestamp}},
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

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {"$set": {"status": "En Route"}},
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
            "$set": {"status": "Completed", "dateCompleted": current_timestamp, "timeSpent": time_spent_str},
            "$push": {
                "invoices": {
                    "invoice_number": f"INV-{jobId[:8].upper()}",
                    "file_path": url_for("download_invoice", filename=filename),
                }
            },
        },
    )

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
        invoice_total = currency_to_float(job.get("total", "$0.00"))
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
        data = request.get_json()
        recipient_email = data.get("recipient_email", "")
        subject = data.get("subject", "")
        body = data.get("body", "")
        estimate_file = data.get("estimate_file", "")
        email_type = str(data.get("email_type", "estimate") or "estimate").strip().lower()

        if not recipient_email or not subject or not body:
            return jsonify({"success": False, "error": "Missing required fields"}), 400

        filename = estimate_file.split("/")[-1]
        base_dir = os.path.dirname(os.path.dirname(__file__))
        invoices_dir = os.path.join(base_dir, "invoices")
        filepath = os.path.join(invoices_dir, filename)

        if not os.path.exists(filepath) or not os.path.abspath(filepath).startswith(os.path.abspath(invoices_dir)):
            return jsonify({"success": False, "error": "Estimate file not found"}), 404

        msg = Message(
            subject=subject,
            recipients=[recipient_email],
            body=body,
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
        total = services_total + parts_total + labor_total + materials_total + equipment_total - discounts_total

        primary_service = services[0]["type"] if services else "No services added."
        scheduled_date = format_date(request.form.get("job_date", ""))
        scheduled_time = request.form.get("job_time", "").strip()
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
            "total": f"${total:.2f}" if total else "$0.00",
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
                    "total": f"${total:.2f}" if total else "$0.00",
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
    selected_property_id = str(job.get("property_id") or "").strip()
    if not selected_property_id:
        default_property = _resolve_default_property(customer)
        selected_property_id = str((default_property or {}).get("property_id") or "").strip()

    return render_template(
        "jobs/update_job.html",
        jobId=jobId,
        job=serialize_doc(job),
        recurrence_state=_build_recurrence_form_state(job=job, series=existing_series),
        recurrence_locked=recurrence_locked,
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
