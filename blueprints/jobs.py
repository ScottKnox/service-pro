import calendar
from datetime import UTC, datetime, timedelta
import copy
import hashlib
import hmac
import json
import os
import secrets

from bson import ObjectId
from flask import Blueprint, current_app, has_request_context, jsonify, redirect, render_template, request, session, url_for
import stripe

from config import get_notification_base_url
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
from utils.notifications import normalize_phone_for_twilio, send_email, send_sms_via_twilio, sms_features_enabled
from utils.qr_codes import generate_payment_qr
from utils.taxes import build_line_item_tax_inputs, calculate_itemized_tax, normalize_business_tax_rates

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

JOB_EQUIPMENT_HVAC_SYSTEM_CONFIG = {
    "AC Condenser": {"system_type": "Split System AC with Gas Furnace", "collection_name": "condensers"},
    "Heat Pump Condenser": {"system_type": "Split System Heat Pump with Air Handler", "collection_name": "condensers"},
    "Gas Furnace": {"system_type": "Split System AC with Gas Furnace", "collection_name": "furnaces"},
    "Air Handler": {"system_type": "Split System Heat Pump with Air Handler", "collection_name": "airHandlers"},
    "Mini Split Outdoor Unit": {"system_type": "Mini Split System", "collection_name": "miniSplits"},
    "Mini Split Indoor Unit": {"system_type": "Mini Split System", "collection_name": "miniSplits"},
    "Package Unit": {"system_type": "Package Unit", "collection_name": "packageUnits"},
}


def _serialize_part_without_legacy_fields(part):
    serialized = serialize_doc(part)
    serialized.pop("manufacturer", None)
    serialized.pop("model_number", None)
    return serialized


def _build_hvac_system_prompt_items(job_doc):
    prompt_items = []
    for index, equipment in enumerate(job_doc.get("equipments") or []):
        if not isinstance(equipment, dict):
            continue

        if str(equipment.get("hvac_system_id") or "").strip():
            continue

        prompt_items.append(
            {
                "index": index,
                "equipment_name": str(equipment.get("equipment_name") or equipment.get("description") or "Equipment").strip() or "Equipment",
                "equipment_type": str(equipment.get("equipment_type") or "").strip() or "Equipment",
                "system_type": JOB_EQUIPMENT_HVAC_SYSTEM_CONFIG.get(str(equipment.get("equipment_type") or "").strip(), {}).get("system_type", "HVAC System"),
                "serial_number": str(equipment.get("serial_number") or "").strip(),
                "manufacturer": str(equipment.get("manufacturer") or "").strip(),
                "model_number": str(equipment.get("model_number") or "").strip(),
                "cooling_capacity": str(equipment.get("cooling_capacity") or "").strip(),
                "seer_rating": str(equipment.get("seer_rating") or "").strip(),
                "metering_device": str(equipment.get("metering_device") or "").strip(),
                "afue_rating": str(equipment.get("afue_rating") or "").strip(),
                "btu_input": str(equipment.get("btu_input") or "").strip(),
                "btu_output": str(equipment.get("btu_output") or "").strip(),
                "refrigerant_type": str(equipment.get("refrigerant_type") or "").strip(),
                "stages": str(equipment.get("stages") or "").strip(),
                "blower_motor_type": str(equipment.get("blower_motor_type") or "").strip(),
                "voltage": str(equipment.get("voltage") or "").strip(),
                "warranty_months": str(equipment.get("warranty_months") or "").strip(),
                "quantity_installed": str(equipment.get("quantity_installed") or equipment.get("quantity") or "").strip() or "1",
            }
        )

    return prompt_items


def _build_hvac_system_creation_payload_from_job_equipment(job, equipment, serial_number):
    equipment_type = str(equipment.get("equipment_type") or "").strip()
    creation_config = JOB_EQUIPMENT_HVAC_SYSTEM_CONFIG.get(equipment_type)
    system_type = creation_config["system_type"] if creation_config else "HVAC System"
    collection_name = creation_config["collection_name"] if creation_config else None
    equipment_name = str(equipment.get("equipment_name") or equipment.get("description") or "Equipment").strip() or "Equipment"
    manufacturer = str(equipment.get("manufacturer") or "").strip()
    model_number = str(equipment.get("model_number") or "").strip()
    current_serial = str(serial_number or equipment.get("serial_number") or "").strip()
    completed_at = job.get("completed_at") if isinstance(job, dict) else None
    if not completed_at:
        completed_at = datetime.now(UTC)

    hvac_system_document = {
        "customer_id": reference_value(job.get("customer_id")),
        "property_id": reference_value(job.get("property_id")) if str(job.get("property_id") or "").strip() else None,
        "system_type": system_type,
        "system_nickname": equipment_name,
        "equipment_type": equipment_type,
        "manufacturer": manufacturer,
        "model_number": model_number,
        "serial_number": current_serial,
        "cooling_capacity": str(equipment.get("cooling_capacity") or "").strip(),
        "seer_rating": str(equipment.get("seer_rating") or "").strip(),
        "metering_device": str(equipment.get("metering_device") or "").strip(),
        "afue_rating": str(equipment.get("afue_rating") or "").strip(),
        "btu_input": str(equipment.get("btu_input") or "").strip(),
        "btu_output": str(equipment.get("btu_output") or "").strip(),
        "refrigerant_type": str(equipment.get("refrigerant_type") or "").strip(),
        "stages": str(equipment.get("stages") or "").strip(),
        "blower_motor_type": str(equipment.get("blower_motor_type") or "").strip(),
        "voltage": str(equipment.get("voltage") or "").strip(),
        "warranty_months": _safe_float(equipment.get("warranty_months") or 0, 0),
        "install_date": completed_at,
        "source_job_id": reference_value(job.get("_id")),
        "source_job_equipment_index": int(equipment.get("source_job_equipment_index") or 0),
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    hvac_system_document = {key: value for key, value in hvac_system_document.items() if value not in {None, ""}}

    if collection_name == "condensers":
        component_document = {
            "serial_number": current_serial,
            "model_number": model_number,
            "manufacturer": manufacturer,
            "manufacturer_other": "",
            "manufactured_year": "",
            "seer_rating": str(equipment.get("seer_rating") or "").strip(),
            "refrigerant_type": str(equipment.get("refrigerant_type") or "").strip(),
            "notes": "Created from job completion.",
        }
    elif collection_name == "furnaces":
        component_document = {
            "serial_number": current_serial,
            "model_number": model_number,
            "manufacturer": manufacturer,
            "manufacturer_other": "",
            "manufactured_year": "",
            "blower_motor_type": str(equipment.get("blower_motor_type") or "").strip(),
            "afue_rating": str(equipment.get("afue_rating") or "").strip(),
            "btu_input": str(equipment.get("btu_input") or "").strip(),
            "btu_output": str(equipment.get("btu_output") or "").strip(),
            "number_of_stages": str(equipment.get("stages") or "").strip(),
            "notes": "Created from job completion.",
        }
    elif collection_name == "airHandlers":
        component_document = {
            "serial_number": current_serial,
            "model_number": model_number,
            "manufacturer": manufacturer,
            "manufacturer_other": "",
            "manufactured_year": "",
            "blower_motor_type": str(equipment.get("blower_motor_type") or "").strip(),
            "notes": "Created from job completion.",
        }
    elif collection_name in {"miniSplits", "packageUnits"}:
        component_document = {
            "serial_number": current_serial,
            "model_number": model_number,
            "manufacturer": manufacturer,
            "manufacturer_other": "",
            "manufactured_year": "",
            "unit_type": equipment_type,
            "seer_rating": str(equipment.get("seer_rating") or "").strip(),
            "notes": "Created from job completion.",
        }
    else:
        component_document = None

    if component_document is not None and collection_name:
        hvac_system_document["components"] = {collection_name: component_document}

    return hvac_system_document


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


def _resolve_business_doc_for_job(db, job_doc):
    business_ref = (job_doc or {}).get("business_id")
    business_query = build_reference_filter("_id", business_ref) if business_ref else None

    business = db.businesses.find_one(business_query) if business_query else None
    if business:
        return business

    fallback_business_id = resolve_current_business_id(db) if has_request_context() else None
    if fallback_business_id:
        return db.businesses.find_one({"_id": fallback_business_id})

    return None


def _build_en_route_sms_message(job_doc, customer_doc, business_doc):
    business_name = str((business_doc or {}).get("company_name") or (business_doc or {}).get("business_name") or "Your HVAC service provider").strip()
    technician_name = str((job_doc or {}).get("assigned_employee") or "Your technician").strip()
    schedule_date = str((job_doc or {}).get("scheduled_date") or "").strip()
    schedule_time = str((job_doc or {}).get("scheduled_time") or "").strip()

    message = f"{business_name}: {technician_name} is en route to your location."
    if schedule_date and schedule_time:
        message += f" Appointment time: {schedule_date} {schedule_time}."
    message += " Reply STOP to unsubscribe."
    return message


def _build_sms_status_event(job_doc, customer_ref, business_doc, from_number, to_number, message_body, message_sid="", message_status="queued"):
    now_utc = datetime.now(UTC)
    return {
        "event_type": "en_route_sms",
        "message_sid": message_sid,
        "message_status": message_status,
        "delivery_status": message_status or "queued",
        "from_number": from_number,
        "to_number": to_number,
        "message_body": message_body,
        "customer_id": str(customer_ref or "").strip(),
        "business_id": str((business_doc or {}).get("_id") or "").strip(),
        "job_id": str((job_doc or {}).get("_id") or "").strip(),
        "created_at": now_utc,
        "updated_at": now_utc,
    }


def _store_sms_event(db, job_doc, sms_event):
    job_id = str((job_doc or {}).get("_id") or "").strip()
    if not job_id or not sms_event:
        return

    db.jobs.update_one(
        {"_id": ObjectId(job_id)},
        {"$push": {"sms_notifications": sms_event}},
    )


def _send_en_route_sms_notification(db, job_doc):
    if not sms_features_enabled():
        current_app.logger.info(
            "En-route SMS skipped: SMS features are disabled by SMS_FEATURES_ENABLED for job_id=%s",
            str((job_doc or {}).get("_id") or ""),
        )
        return

    customer_ref = (job_doc or {}).get("customer_id")
    if not customer_ref:
        return

    customer_doc = db.customers.find_one(build_reference_filter("_id", customer_ref)) or {}
    business_doc = _resolve_business_doc_for_job(db, job_doc) or {}

    customer_phone = normalize_phone_for_twilio(customer_doc.get("phone") or customer_doc.get("phone_number"))
    twilio_from_phone = normalize_phone_for_twilio((business_doc or {}).get("twilio_phone_number"))
    if not customer_phone:
        current_app.logger.warning("En-route SMS skipped: missing customer phone for job_id=%s", str((job_doc or {}).get("_id") or ""))
        return
    if not twilio_from_phone:
        current_app.logger.warning("En-route SMS skipped: missing business Twilio phone number for job_id=%s", str((job_doc or {}).get("_id") or ""))
        return

    message_body = _build_en_route_sms_message(job_doc, customer_doc, business_doc)
    status_callback_url = _build_notification_url(
        "jobs.twilio_sms_status_callback",
        external=True,
        jobId=str((job_doc or {}).get("_id") or ""),
    )
    sent, detail = send_sms_via_twilio(
        to_number=customer_phone,
        from_number=twilio_from_phone,
        message_body=message_body,
        status_callback_url=status_callback_url,
    )

    if sent:
        message_sid = str((detail or {}).get("sid") or "").strip()
        message_status = str((detail or {}).get("status") or "queued").strip() or "queued"
        _store_sms_event(
            db,
            job_doc,
            _build_sms_status_event(
                job_doc,
                customer_ref,
                business_doc,
                twilio_from_phone,
                customer_phone,
                message_body,
                message_sid=message_sid,
                message_status=message_status,
            ),
        )
        current_app.logger.info(
            "En-route SMS sent: job_id=%s customer_id=%s twilio_sid=%s",
            str((job_doc or {}).get("_id") or ""),
            str(customer_ref or ""),
            message_sid,
        )
        return

    current_app.logger.error(
        "En-route SMS failed: job_id=%s customer_id=%s error=%s",
        str((job_doc or {}).get("_id") or ""),
        str(customer_ref or ""),
        detail,
    )


def _twilio_request_signature_valid(expected_url):
    """Validate the X-Twilio-Signature header against the configured auth token.

    Twilio signs each request with the account auth token and the exact callback
    URL it was given. Verifying the signature ensures the request genuinely came
    from Twilio (replacing CSRF protection, which Twilio cannot satisfy). If no
    auth token is configured, SMS is not set up and there is nothing to verify.
    """
    auth_token = str(os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    if not auth_token:
        return True

    try:
        from twilio.request_validator import RequestValidator
    except ImportError:
        current_app.logger.warning(
            "Twilio library unavailable; cannot validate callback signature"
        )
        return False

    signature = request.headers.get("X-Twilio-Signature", "")
    validator = RequestValidator(auth_token)
    return validator.validate(expected_url, request.form.to_dict(flat=True), signature)


@bp.route("/twilio/sms/status/<jobId>", methods=["POST"])
def twilio_sms_status_callback(jobId):
    expected_url = _build_notification_url(
        "jobs.twilio_sms_status_callback", external=True, jobId=jobId
    )
    if not _twilio_request_signature_valid(expected_url):
        current_app.logger.warning(
            "Rejected Twilio status callback with invalid signature for job_id=%s", jobId
        )
        return "", 403

    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)}, {"sms_notifications": 1})
    if not job:
        return "", 404

    message_sid = str(request.form.get("MessageSid") or request.values.get("MessageSid") or "").strip()
    message_status = str(request.form.get("MessageStatus") or request.values.get("MessageStatus") or "").strip().lower()
    error_code = str(request.form.get("ErrorCode") or request.values.get("ErrorCode") or "").strip()
    error_message = str(request.form.get("ErrorMessage") or request.values.get("ErrorMessage") or "").strip()

    if not message_sid:
        return "", 400

    now_utc = datetime.now(UTC)
    updated_notifications = []
    matched = False
    for entry in list(job.get("sms_notifications") or []):
        if not isinstance(entry, dict):
            updated_notifications.append(entry)
            continue

        updated_entry = dict(entry)
        if str(updated_entry.get("message_sid") or "").strip() == message_sid:
            matched = True
            if message_status:
                updated_entry["message_status"] = message_status
                updated_entry["delivery_status"] = message_status
            if error_code:
                updated_entry["error_code"] = error_code
            if error_message:
                updated_entry["error_message"] = error_message
            updated_entry["updated_at"] = now_utc
            if message_status in {"delivered", "sent", "read"}:
                updated_entry["delivered_at"] = now_utc
        updated_notifications.append(updated_entry)

    if matched:
        db.jobs.update_one(
            {"_id": ObjectId(jobId)},
            {
                "$set": {
                    "sms_notifications": updated_notifications,
                    "updated_at": now_utc,
                }
            },
        )

    current_app.logger.info(
        "Twilio SMS status callback: job_id=%s message_sid=%s status=%s error_code=%s",
        jobId,
        message_sid,
        message_status,
        error_code,
    )
    return "", 204


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


def _build_recurring_series_document(
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
    technician_payload,
    recurring_data,
    scheduled_date,
    scheduled_time,
    payment_due_days_offset,
    request_obj,
):
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
        "primary_technician_id": technician_payload.get("primary_technician_id") or None,
        "additional_technician_ids": list(technician_payload.get("additional_technician_ids") or []),
        "additional_technician_names": list(technician_payload.get("additional_technician_names") or []),
        "assigned_employee": str(technician_payload.get("assigned_employee") or "").strip(),
        "total_amount": float(total or 0.0),
        "invoice_notes": request_obj.form.get("invoice_notes", "").strip(),
        "payment_due_days_offset": recurring_due_offset,
        "business_id": business_id,
        "created_at": datetime.now(UTC),
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
        "status": resolve_job_status(
            scheduled_date,
            scheduled_time,
            services,
            parts,
            labors,
            materials,
            equipments,
            discounts,
            primary_technician_id=str(series_doc.get("primary_technician_id") or "").strip(),
        ),
        "scheduled_date": scheduled_date,
        "scheduled_time": scheduled_time,
        "dateScheduled": datetime.now().strftime("%m/%d/%Y") if (scheduled_date and scheduled_time) else "",
        "address_line_1": str(series_doc.get("address_line_1") or "").strip(),
        "address_line_2": str(series_doc.get("address_line_2") or "").strip(),
        "city": str(series_doc.get("city") or "").strip(),
        "state": str(series_doc.get("state") or "").strip().upper(),
        "zip_code": str(series_doc.get("zip_code") or "").strip(),
        "primary_technician_id": str(series_doc.get("primary_technician_id") or "").strip() or None,
        "additional_technician_ids": list(series_doc.get("additional_technician_ids") or []),
        "additional_technician_names": list(series_doc.get("additional_technician_names") or []),
        "assigned_employee": str(series_doc.get("assigned_employee") or "").strip(),
        "total_amount": float(series_doc.get("total_amount") or 0.0),
        "invoice_notes": str(series_doc.get("invoice_notes") or "").strip(),
        "payment_due_days": payment_due_days,
        "internal_notes": [],
        "date_created": datetime.now().strftime("%m/%d/%Y"),
        "created_at": datetime.now(UTC),
        "invoices": [],
        "total_amount_paid": 0.0,
        "balance_due": float(series_doc.get("total_amount") or 0.0),
        "payment_status": "pending_paid",
        "paid_at": None,
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
    return str((business or {}).get("custom_logo") or "").strip()


def resolve_job_status(scheduled_date, scheduled_time, services, parts, labors, materials, equipments, discounts, existing_status="", primary_technician_id=""):
    """Derive job status from scheduling and line items while preserving terminal states."""
    normalized_existing = str(existing_status or "").strip().lower()
    if normalized_existing in {"started", "completed", "paid"}:
        return str(existing_status)

    has_schedule = bool(str(scheduled_date).strip()) and bool(str(scheduled_time).strip())
    has_primary_technician = bool(str(primary_technician_id or "").strip())

    if has_schedule and has_primary_technician:
        return "Scheduled"
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


def _hydrate_service_descriptions_for_pdf(db, payload, business_id=None):
    hydrated_payload = dict(payload or {})
    service_rows = hydrated_payload.get("services") or []
    if not service_rows:
        return hydrated_payload

    service_codes = set()
    service_names = set()
    for service in service_rows:
        if not isinstance(service, dict):
            continue
        if str(service.get("description") or service.get("service_description") or "").strip():
            continue

        service_code = str(service.get("code") or service.get("service_code") or "").strip()
        if service_code:
            service_codes.add(service_code)
            continue

        service_name = str(
            service.get("service_name")
            or service.get("name")
            or service.get("type")
            or ""
        ).strip()
        if service_name:
            service_names.add(service_name)

    if not service_codes and not service_names:
        return hydrated_payload

    query = {}
    if business_id:
        query["business_id"] = business_id

    or_filters = []
    if service_codes:
        or_filters.append({"service_code": {"$in": sorted(service_codes)}})
    if service_names:
        or_filters.append({"service_name": {"$in": sorted(service_names)}})
        or_filters.append({"name": {"$in": sorted(service_names)}})
    if not or_filters:
        return hydrated_payload
    query["$or"] = or_filters

    description_by_code = {}
    description_by_name = {}
    for service_doc in db.services.find(query, {"service_code": 1, "service_name": 1, "name": 1, "description": 1}):
        description = str(service_doc.get("description") or "").strip()
        if not description:
            continue
        service_code = str(service_doc.get("service_code") or "").strip()
        service_name = str(service_doc.get("service_name") or service_doc.get("name") or "").strip()
        if service_code:
            description_by_code[service_code] = description
        if service_name:
            description_by_name[service_name] = description

    hydrated_services = []
    for service in service_rows:
        if not isinstance(service, dict):
            hydrated_services.append(service)
            continue

        hydrated_service = dict(service)
        existing_description = str(hydrated_service.get("description") or hydrated_service.get("service_description") or "").strip()
        if not existing_description:
            service_code = str(hydrated_service.get("code") or hydrated_service.get("service_code") or "").strip()
            service_name = str(
                hydrated_service.get("service_name")
                or hydrated_service.get("name")
                or hydrated_service.get("type")
                or ""
            ).strip()
            hydrated_service["description"] = (
                description_by_code.get(service_code)
                or description_by_name.get(service_name)
                or ""
            )

        hydrated_services.append(hydrated_service)

    hydrated_payload["services"] = hydrated_services
    return hydrated_payload


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


def _parse_payment_schedule_payload(raw_value, default=None):
    if raw_value in (None, ""):
        return default if default is not None else []

    if isinstance(raw_value, list):
        return raw_value

    if isinstance(raw_value, dict):
        return [raw_value]

    if not isinstance(raw_value, str):
        return default if default is not None else []

    text = raw_value.strip()
    if not text:
        return default if default is not None else []

    try:
        parsed = json.loads(text)
    except Exception:
        return default if default is not None else []

    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return default if default is not None else []


def _extract_primary_service_category(services):
    for service in services or []:
        if not isinstance(service, dict):
            continue
        category = str(service.get("category") or service.get("service_category") or "").strip()
        if category:
            return category
    return ""


def _resolve_payment_schedule_templates(business_doc):
    raw_templates = (business_doc or {}).get("payment_schedule_templates")
    return _parse_payment_schedule_payload(raw_templates, default=[])


def _find_payment_schedule_template(business_doc, category_name):
    normalized_category = str(category_name or "").strip().lower()
    if not normalized_category:
        return None

    for template in _resolve_payment_schedule_templates(business_doc):
        if not isinstance(template, dict):
            continue
        template_category = str(template.get("category") or template.get("service_category") or template.get("name") or "").strip().lower()
        if template_category == normalized_category:
            return template
    return None


def _normalize_payment_schedule_stages(stages, total_amount=0.0, preserve_stage_ids=False):
    normalized_stages = []
    stage_rows = [stage for stage in (stages or []) if isinstance(stage, dict)]
    total_due = round(_safe_float(total_amount, 0.0), 2)
    running_amount = 0.0

    for stage in stage_rows:
        stage_name = str(stage.get("name") or "Stage").strip() or "Stage"
        amount_type = str(stage.get("amount_type") or "").strip().lower()
        if amount_type not in {"percentage", "fixed", "remaining"}:
            if stage_name == "Final Payment":
                amount_type = "remaining"
            elif stage.get("percentage") not in [None, ""]:
                amount_type = "percentage"
            else:
                amount_type = "fixed"

        raw_amount_value = stage.get("amount_value")
        if raw_amount_value in [None, ""]:
            if amount_type == "percentage":
                raw_amount_value = stage.get("percentage") if stage.get("percentage") not in [None, ""] else stage.get("amount")
            elif amount_type == "fixed":
                raw_amount_value = stage.get("amount")

        amount_value = None
        if amount_type != "remaining":
            amount_value = round(max(0.0, _safe_float(raw_amount_value, 0.0)), 2)

        if amount_type == "percentage":
            effective_percentage = max(0.0, amount_value or 0.0)
            amount = round(total_due * (effective_percentage / 100.0), 2) if total_due > 0 else 0.0
        elif amount_type == "fixed":
            amount = round(max(0.0, amount_value or 0.0), 2)
            effective_percentage = round((amount / total_due) * 100.0, 2) if total_due > 0 and amount > 0 else 0.0
        else:
            amount = round(max(0.0, total_due - running_amount), 2) if total_due > 0 else 0.0
            effective_percentage = round((amount / total_due) * 100.0, 2) if total_due > 0 and amount > 0 else 0.0

        running_amount += amount

        status = str(stage.get("status") or "pending").strip().lower()
        if amount <= 0 and status not in {"paid", "cancelled"}:
            status = "paid"

        due_at = _coerce_datetime_utc(stage.get("due_at"))
        paid_at = _coerce_datetime_utc(stage.get("paid_at"))
        request_sent_at = _coerce_datetime_utc(stage.get("request_sent_at"))

        normalized_stage = {
            "stage_id": str(stage.get("stage_id") or "").strip() if preserve_stage_ids else str(stage.get("stage_id") or "").strip(),
            "name": stage_name,
            "amount_type": amount_type,
            "amount_value": None if amount_type == "remaining" else amount_value,
            "amount": round(amount, 2),
            "percentage": round(effective_percentage, 2),
            "trigger": str(stage.get("trigger") or "manual").strip() or "manual",
            "send_payment_request": bool(stage.get("send_payment_request", True)),
            "status": status,
            "due_at": due_at,
            "paid_at": paid_at,
            "payment_id": str(stage.get("payment_id") or "").strip() or None,
            "request_sent_at": request_sent_at,
            "trigger_fired_at": _coerce_datetime_utc(stage.get("trigger_fired_at")),
        }
        if not normalized_stage["stage_id"]:
            normalized_stage["stage_id"] = secrets.token_hex(8)
        normalized_stages.append(normalized_stage)

    return normalized_stages


def _build_payment_schedule_from_template(template, total_amount):
    template_stages = [stage for stage in (template or {}).get("stages") or [] if isinstance(stage, dict)]
    staged = []
    for stage in template_stages:
        amount_type = str(stage.get("amount_type") or "").strip().lower()
        if amount_type not in {"percentage", "fixed", "remaining"}:
            if str(stage.get("name") or "").strip() == "Final Payment":
                amount_type = "remaining"
            elif stage.get("percentage") not in [None, ""]:
                amount_type = "percentage"
            else:
                amount_type = "fixed"

        amount_value = None
        if amount_type == "percentage":
            amount_value = round(max(0.0, _safe_float(stage.get("amount") if stage.get("amount") not in [None, ""] else stage.get("percentage"), 0.0)), 2)
        elif amount_type == "fixed":
            amount_value = round(max(0.0, _safe_float(stage.get("amount"), 0.0)), 2)

        staged.append(
            {
                "stage_id": secrets.token_hex(8),
                "name": str(stage.get("name") or "Stage").strip() or "Stage",
                "amount_type": amount_type,
                "amount_value": amount_value,
                "amount": 0.0,
                "trigger": str(stage.get("trigger") or "manual").strip() or "manual",
                "send_payment_request": bool(stage.get("send_payment_request", True)),
                "status": "pending",
                "due_at": None,
                "paid_at": None,
                "payment_id": None,
                "request_sent_at": None,
                "trigger_fired_at": None,
            }
        )

    return _normalize_payment_schedule_stages(staged, total_amount=total_amount, preserve_stage_ids=True)


def _build_payment_schedule_for_record(record, business_doc=None, total_amount=0.0, raw_schedule=None):
    existing_schedule = _parse_payment_schedule_payload(raw_schedule, default=None)
    if existing_schedule is None:
        existing_schedule = _parse_payment_schedule_payload((record or {}).get("payment_schedule"), default=None)

    if existing_schedule:
        return _normalize_payment_schedule_stages(existing_schedule, total_amount=total_amount, preserve_stage_ids=True)

    primary_category = _extract_primary_service_category((record or {}).get("services") or [])
    template = _find_payment_schedule_template(business_doc or {}, primary_category)
    if template:
        return _build_payment_schedule_from_template(template, total_amount)

    return []


def _allocate_payments_to_payment_schedule(payment_schedule, payment_docs):
    stages = _normalize_payment_schedule_stages(payment_schedule or [], total_amount=sum(_safe_float(stage.get("amount"), 0.0) for stage in (payment_schedule or [])), preserve_stage_ids=True)
    if not stages:
        return []

    payments = []
    for payment in payment_docs or []:
        if not isinstance(payment, dict):
            continue
        if str(payment.get("status") or "").strip().lower() != "completed":
            continue
        paid_at = _coerce_datetime_utc(payment.get("paid_at")) or _coerce_datetime_utc(payment.get("created_at")) or datetime.now(UTC)
        payments.append(
            {
                "payment_id": str(payment.get("_id") or "").strip(),
                "amount": round(_safe_float(payment.get("amount"), 0.0), 2),
                "paid_at": paid_at,
            }
        )

    payments.sort(key=lambda row: row["paid_at"])
    stage_index = 0
    amount_cursor = 0.0
    for payment in payments:
        remaining_payment = payment["amount"]
        while remaining_payment > 0 and stage_index < len(stages):
            stage = stages[stage_index]
            if str(stage.get("status") or "").strip().lower() == "cancelled":
                stage_index += 1
                continue

            stage_amount = round(_safe_float(stage.get("amount"), 0.0), 2)
            if stage_amount <= 0:
                stage.update(
                    {
                        "status": "paid",
                        "paid_at": payment["paid_at"],
                        "payment_id": payment["payment_id"] or stage.get("payment_id"),
                        "request_sent_at": stage.get("request_sent_at"),
                    }
                )
                stage_index += 1
                continue

            already_paid = _safe_float(stage.get("amount_paid"), 0.0)
            outstanding = round(max(0.0, stage_amount - already_paid), 2)
            allocated = round(min(outstanding, remaining_payment), 2)
            if allocated <= 0:
                stage_index += 1
                continue

            new_paid = round(already_paid + allocated, 2)
            new_remaining = round(max(0.0, stage_amount - new_paid), 2)
            stage["amount_paid"] = new_paid
            stage["amount_remaining"] = new_remaining
            stage["payment_id"] = payment["payment_id"] or stage.get("payment_id")
            if new_remaining <= 0:
                stage["status"] = "paid"
                stage["paid_at"] = payment["paid_at"]
                stage_index += 1
            else:
                stage["status"] = "partial"
                stage["paid_at"] = None

            remaining_payment = round(remaining_payment - allocated, 2)
            amount_cursor += allocated

        if stage_index >= len(stages):
            break

    for stage in stages:
        amount_paid = round(_safe_float(stage.get("amount_paid"), 0.0), 2)
        amount_total = round(_safe_float(stage.get("amount"), 0.0), 2)
        stage["amount_remaining"] = round(max(0.0, amount_total - amount_paid), 2)
        if amount_total <= 0 and str(stage.get("status") or "").strip().lower() not in {"cancelled"}:
            stage["status"] = "paid"
            if not stage.get("paid_at"):
                stage["paid_at"] = stage.get("trigger_fired_at") or datetime.now(UTC)
    return stages


def _sync_job_payment_schedule(db, job_id):
    if not ObjectId.is_valid(job_id):
        return None

    job = db.jobs.find_one({"_id": ObjectId(job_id)})
    if not job:
        return None

    payment_schedule = job.get("payment_schedule") or []
    if not payment_schedule:
        return None

    payment_docs = list(db.payments.find(_reference_match("job_id", job_id)).sort([("paid_at", 1), ("created_at", 1), ("_id", 1)]))
    recalculated_schedule = _allocate_payments_to_payment_schedule(payment_schedule, payment_docs)
    db.jobs.update_one(
        {"_id": ObjectId(job_id)},
        {"$set": {"payment_schedule": recalculated_schedule, "updated_at": datetime.now(UTC)}},
    )
    refreshed = db.jobs.find_one({"_id": ObjectId(job_id)})
    return refreshed


def _fire_payment_schedule_trigger(payment_schedule, trigger_name, fired_at=None):
    updated_schedule = []
    trigger_matches = 0
    fired_timestamp = fired_at or datetime.now(UTC)
    normalized_trigger = str(trigger_name or "").strip().lower()

    for stage in payment_schedule or []:
        if not isinstance(stage, dict):
            continue

        normalized_stage = dict(stage)
        stage_trigger = str(normalized_stage.get("trigger") or "manual").strip().lower()
        stage_status = str(normalized_stage.get("status") or "pending").strip().lower()

        if stage_status in {"paid", "cancelled"}:
            updated_schedule.append(normalized_stage)
            continue

        if stage_trigger == normalized_trigger:
            trigger_matches += 1
            normalized_stage["trigger_fired_at"] = fired_timestamp
            normalized_stage["due_at"] = fired_timestamp
            if _safe_float(normalized_stage.get("amount"), 0.0) <= 0:
                normalized_stage["status"] = "paid"
                normalized_stage["paid_at"] = fired_timestamp
            else:
                normalized_stage["status"] = "due"
        updated_schedule.append(normalized_stage)

    return updated_schedule, trigger_matches > 0


def _send_triggered_payment_schedule_requests(db, job_id, trigger_name):
    if not job_id or not ObjectId.is_valid(job_id):
        return 0

    job = db.jobs.find_one({"_id": ObjectId(job_id)})
    if not job:
        return 0

    job_doc = serialize_doc(job)
    sent_count = 0
    normalized_trigger = str(trigger_name or "").strip().lower()
    for stage in job_doc.get("payment_schedule") or []:
        if not isinstance(stage, dict):
            continue
        if str(stage.get("trigger") or "").strip().lower() != normalized_trigger:
            continue
        if not bool(stage.get("send_payment_request", True)):
            continue
        if str(stage.get("status") or "").strip().lower() not in {"due", "partial"}:
            continue
        if stage.get("request_sent_at"):
            continue
        ok, detail = _send_payment_schedule_stage_request(db, job_id, str(stage.get("stage_id") or "").strip())
        if ok:
            sent_count += 1
        else:
            current_app.logger.info(
                "Payment schedule request not sent: job_id=%s stage_id=%s trigger=%s detail=%s",
                job_id,
                str(stage.get("stage_id") or "").strip(),
                normalized_trigger,
                detail,
            )
    return sent_count


def _build_payment_schedule_view(payment_schedule):
    rows = []
    total_amount = 0.0
    total_paid = 0.0
    for stage in payment_schedule or []:
        if not isinstance(stage, dict):
            continue
        amount = round(_safe_float(stage.get("amount"), 0.0), 2)
        amount_paid = round(_safe_float(stage.get("amount_paid"), 0.0), 2)
        total_amount += amount
        total_paid += amount_paid if amount_paid > 0 else (amount if str(stage.get("status") or "").strip().lower() == "paid" else 0.0)
        rows.append(
            {
                "stage_id": str(stage.get("stage_id") or "").strip(),
                "name": str(stage.get("name") or "Stage").strip() or "Stage",
                "amount_type": str(stage.get("amount_type") or "fixed").strip().lower() or "fixed",
                "amount_value": stage.get("amount_value"),
                "trigger": str(stage.get("trigger") or "manual").strip() or "manual",
                "status": str(stage.get("status") or "pending").strip().lower() or "pending",
                "amount": amount,
                "amount_display": normalize_currency(amount),
                "amount_paid": amount_paid,
                "amount_remaining": round(_safe_float(stage.get("amount_remaining"), max(0.0, amount - amount_paid)), 2),
                "percentage": round(_safe_float(stage.get("percentage"), 0.0), 2),
                "send_payment_request": bool(stage.get("send_payment_request", True)),
                "due_at": stage.get("due_at"),
                "paid_at": stage.get("paid_at"),
                "request_sent_at": stage.get("request_sent_at"),
            }
        )

    return {
        "stages": rows,
        "total_amount": round(total_amount, 2),
        "amount_paid": round(total_paid, 2),
        "balance_due": round(max(0.0, total_amount - total_paid), 2),
        "has_schedule": bool(rows),
    }


def _payment_schedule_due_now_amount(payment_schedule):
    due_now_total = 0.0
    for stage in payment_schedule or []:
        if not isinstance(stage, dict):
            continue
        status = str(stage.get("status") or "").strip().lower()
        if status not in {"due", "partial"}:
            continue
        if status == "partial":
            due_now_total += max(0.0, _safe_float(stage.get("amount_remaining"), _safe_float(stage.get("amount"), 0.0)))
        else:
            due_now_total += max(0.0, _safe_float(stage.get("amount"), 0.0))
    return round(due_now_total, 2)


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
    if not business_id:
        # Fallback for customer-initiated acceptance (no staff session): find the sole business.
        sole_business = db.businesses.find_one({}, {"_id": 1})
        if sole_business:
            business_id = sole_business["_id"]
    business_doc = {}
    if business_id:
        business_doc = serialize_doc(db.businesses.find_one(build_reference_filter("_id", business_id)) or {})
    payment_schedule = _build_payment_schedule_for_record(
        {"services": services, "payment_schedule": estimate.get("payment_schedule") or []},
        business_doc=business_doc,
        total_amount=float(estimate.get("total_amount") or 0.0),
        raw_schedule="",
    )

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
        "payment_schedule": payment_schedule,
        "internal_notes": [],
        "date_created": datetime.now().strftime("%m/%d/%Y"),
        "created_at": datetime.now(UTC),
        "invoices": [],
        "total_amount_paid": 0.0,
        "balance_due": float(estimate.get("total_amount") or 0.0),
        "payment_status": "pending_paid",
        "paid_at": None,
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
        "maintenance_plan_id": estimate.get("maintenance_plan_id"),
        "is_maintenance_visit": bool(estimate.get("is_maintenance_visit")),
        "plan_discount_applied": bool(estimate.get("plan_discount_applied")),
        "plan_discount_pct": estimate.get("plan_discount_pct"),
    }

    inserted = db.jobs.insert_one(new_job)
    if payment_schedule:
        _ensure_job_invoice_entry(db, str(inserted.inserted_id))
    created_job_id = str(inserted.inserted_id)
    db.estimates.update_one(
        {"_id": ObjectId(estimate_id)},
        {
            "$set": {
                "created_job_id": created_job_id,
                "job_created_from_estimate_at": datetime.now(UTC),
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
    employee_docs = []
    for employee in db.employees.find(_employee_business_scope(db)).sort([("last_name", 1), ("first_name", 1)]):
        serialized_employee = serialize_doc(employee)
        if str(serialized_employee.get("status") or "active").strip().lower() != "active":
            continue
        employee_docs.append(serialized_employee)

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
    session_business_id = str(session.get("employee_business_id") or "").strip()
    if ObjectId.is_valid(session_business_id):
        return ObjectId(session_business_id)

    employee_id = session.get("employee_id")
    if not employee_id or not ObjectId.is_valid(employee_id):
        return None

    employee = db.employees.find_one({"_id": ObjectId(employee_id)}, {"business": 1, "business_id": 1, "company_id": 1})
    for business_field in ("business", "business_id", "company_id"):
        business_ref = (employee or {}).get(business_field)
        if isinstance(business_ref, ObjectId):
            return business_ref
        if isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
            return ObjectId(business_ref)
    return None


def _doc_belongs_to_current_business(db, doc):
    """Defense-in-depth tenant check for job/estimate documents.

    Returns True when the document's ``business_id`` matches the current
    employee's business. Enforcement is skipped only when the current business
    cannot be resolved; documents created by the app always carry
    ``business_id``, so this blocks cross-tenant access by direct ID.
    """
    business_id = resolve_current_business_id(db)
    if not business_id:
        return True
    return str((doc or {}).get("business_id") or "").strip() == str(business_id)


def _employee_business_scope(db):
    """Return a Mongo filter restricting employees to the current business.

    Employee business ownership may be stored under ``business`` (preferred) or
    the legacy ``business_id``/``company_id`` fields, as either an ObjectId or a
    string. When the current business cannot be resolved we return a
    match-nothing filter so employees from other tenants are never exposed.
    """
    business_id = resolve_current_business_id(db)
    if not business_id:
        return {"_id": {"$in": []}}

    business_str = str(business_id)
    predicates = []
    for field_name in ("business", "business_id", "company_id"):
        predicates.append({field_name: business_id})
        predicates.append({field_name: business_str})
    return {"$or": predicates}


def _scoped_employee_filter(db, base_filter=None):
    """Combine ``base_filter`` with the current-business employee scope."""
    scope = _employee_business_scope(db)
    if not base_filter:
        return scope
    return {"$and": [dict(base_filter), scope]}


def _is_authenticated_employee():
    employee_id = session.get("employee_id")
    return bool(employee_id and ObjectId.is_valid(employee_id))


@bp.route("/jobs/<jobId>/dispatch-assign", methods=["POST"])
def dispatch_assign_job(jobId):
    db = ensure_connection_or_500()
    if not _is_authenticated_employee():
        return jsonify({"success": False, "error": "Authentication required"}), 401

    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404

    business_id = resolve_current_business_id(db)
    if business_id and str(job.get("business_id") or "").strip() != str(business_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403

    payload = request.get_json(silent=True) or request.form
    primary_technician_raw = str((payload or {}).get("primary_technician_id") or "").strip()
    scheduled_date_iso = str((payload or {}).get("scheduled_date") or "").strip()
    scheduled_time = str((payload or {}).get("scheduled_time") or "").strip()

    if not primary_technician_raw or not scheduled_date_iso or not scheduled_time:
        return jsonify({"success": False, "error": "Primary technician, date, and time are required"}), 400

    try:
        scheduled_date = datetime.strptime(scheduled_date_iso, "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return jsonify({"success": False, "error": "Invalid scheduled date format"}), 400

    try:
        normalized_time = datetime.strptime(scheduled_time, "%H:%M")
        scheduled_time = normalized_time.strftime("%H:%M")
    except ValueError:
        return jsonify({"success": False, "error": "Invalid scheduled time format"}), 400

    technician_payload = _build_job_technician_payload(
        db,
        primary_technician_raw,
        list(job.get("additional_technician_ids") or []),
    )
    if not technician_payload.get("primary_technician_id"):
        return jsonify({"success": False, "error": "Selected technician is not active"}), 400

    serialized_job = serialize_doc(job)
    status_value = resolve_job_status(
        scheduled_date,
        scheduled_time,
        serialized_job.get("services") or [],
        serialized_job.get("parts") or [],
        serialized_job.get("labors") or [],
        serialized_job.get("materials") or [],
        serialized_job.get("equipments") or [],
        serialized_job.get("discounts") or [],
        existing_status=str(serialized_job.get("status") or "").strip(),
        primary_technician_id=technician_payload.get("primary_technician_id") or "",
    )

    now_utc = datetime.now(UTC)
    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {
            "$set": {
                "primary_technician_id": technician_payload.get("primary_technician_id") or None,
                "additional_technician_ids": technician_payload.get("additional_technician_ids") or [],
                "additional_technician_names": technician_payload.get("additional_technician_names") or [],
                "assigned_employee": technician_payload.get("assigned_employee") or "",
                "scheduled_date": scheduled_date,
                "scheduled_time": scheduled_time,
                "dateScheduled": now_utc.strftime("%m/%d/%Y"),
                "status": status_value,
                "updated_at": now_utc,
            }
        },
    )

    updated_job = serialize_doc(db.jobs.find_one({"_id": ObjectId(jobId)}) or {})
    services = list(updated_job.get("services") or [])
    primary_service_name = ""
    primary_service_category = ""
    all_service_names = []
    for service in services:
        if not isinstance(service, dict):
            continue
        label = ""
        for field_name in ("type", "service_name", "name", "service_code", "code", "description"):
            candidate = str(service.get(field_name) or "").strip()
            if candidate:
                label = candidate
                break
        if label:
            all_service_names.append(label)
        if not primary_service_name and label:
            primary_service_name = label
            primary_service_category = str(service.get("category") or service.get("service_category") or "").strip()

    try:
        start_minutes = int(scheduled_time.split(":", 1)[0]) * 60 + int(scheduled_time.split(":", 1)[1])
    except (ValueError, IndexError):
        start_minutes = 0

    return jsonify(
        {
            "success": True,
            "job": {
                "id": str(updated_job.get("_id") or "").strip(),
                "customer_name": str(updated_job.get("customer_name") or "Unknown Customer").strip() or "Unknown Customer",
                "primary_service_name": primary_service_name,
                "primary_service_category": primary_service_category,
                "status": str(updated_job.get("status") or "Scheduled").strip() or "Scheduled",
                "address": ", ".join(
                    [
                        part
                        for part in [
                            str(updated_job.get("address_line_1") or "").strip(),
                            str(updated_job.get("city") or "").strip(),
                            str(updated_job.get("state") or "").strip(),
                            str(updated_job.get("zip_code") or "").strip(),
                        ]
                        if part
                    ]
                ),
                "scheduled_date": scheduled_date_iso,
                "scheduled_time": scheduled_time,
                "start_minutes": start_minutes,
                "duration_minutes": 45,
                "primary_technician_id": str(updated_job.get("primary_technician_id") or "").strip(),
                "assigned_employee": str(updated_job.get("assigned_employee") or "").strip(),
                "view_url": url_for("jobs.view_job", jobId=str(updated_job.get("_id") or "").strip()),
                "all_service_names": all_service_names,
            },
        }
    )


def _coerce_business_object_id(value):
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    return None


def _employee_lookup(db):
    lookup = {}
    for employee in db.employees.find(_employee_business_scope(db)).sort([("last_name", 1), ("first_name", 1)]):
        serialized_employee = serialize_doc(employee)
        employee_id = str(serialized_employee.get("_id") or "").strip()
        if not employee_id or str(serialized_employee.get("status") or "active").strip().lower() != "active":
            continue
        first_name = str(serialized_employee.get("first_name") or "").strip()
        last_name = str(serialized_employee.get("last_name") or "").strip()
        full_name = f"{first_name} {last_name}".strip()
        if full_name:
            lookup[employee_id] = full_name
    return lookup


def _resolve_employee_id_value(db, value):
    raw_value = str(value or "").strip()
    if not raw_value:
        return ""
    if ObjectId.is_valid(raw_value):
        employee = db.employees.find_one(
            _scoped_employee_filter(db, {"_id": ObjectId(raw_value)}), {"status": 1}
        )
        if employee and str(employee.get("status") or "active").strip().lower() == "active":
            return raw_value
        return ""

    normalized_name = " ".join(raw_value.lower().split())
    if not normalized_name:
        return ""

    for employee in db.employees.find(_employee_business_scope(db), {"first_name": 1, "last_name": 1, "status": 1}):
        if str(employee.get("status") or "active").strip().lower() != "active":
            continue
        full_name = " ".join(f"{str(employee.get('first_name') or '').strip()} {str(employee.get('last_name') or '').strip()}".split()).lower()
        if full_name == normalized_name:
            return str(employee.get("_id") or "").strip()
    return ""


def _resolve_employee_name_from_value(db, value):
    employee_id = _resolve_employee_id_value(db, value)
    if not employee_id:
        return ""
    employee = db.employees.find_one(
        _scoped_employee_filter(db, {"_id": ObjectId(employee_id)}), {"first_name": 1, "last_name": 1}
    ) or {}
    first_name = str(employee.get("first_name") or "").strip()
    last_name = str(employee.get("last_name") or "").strip()
    return f"{first_name} {last_name}".strip()


def _sanitize_additional_technician_ids(db, additional_ids, primary_technician_id=""):
    primary_id = str(primary_technician_id or "").strip()
    cleaned_ids = []
    seen_ids = set()
    for raw_id in additional_ids or []:
        employee_id = _resolve_employee_id_value(db, raw_id)
        if not employee_id or employee_id == primary_id or employee_id in seen_ids:
            continue
        seen_ids.add(employee_id)
        cleaned_ids.append(employee_id)
    return cleaned_ids


def _build_job_technician_payload(db, primary_technician_id="", additional_technician_ids=None):
    primary_employee_id = _resolve_employee_id_value(db, primary_technician_id)
    primary_employee_name = _resolve_employee_name_from_value(db, primary_employee_id)
    cleaned_additional_ids = _sanitize_additional_technician_ids(db, additional_technician_ids or [], primary_employee_id)
    lookup = _employee_lookup(db)
    additional_names = [lookup.get(employee_id, "") for employee_id in cleaned_additional_ids if lookup.get(employee_id)]
    return {
        "primary_technician_id": primary_employee_id or None,
        "additional_technician_ids": cleaned_additional_ids,
        "additional_technician_names": additional_names,
        "assigned_employee": primary_employee_name,
    }


def _employee_has_access_to_job(db, job_doc):
    job_business_id = _coerce_business_object_id((job_doc or {}).get("business_id"))
    if not job_business_id:
        return False

    employee_business_id = resolve_current_business_id(db)
    if not employee_business_id:
        return False

    return job_business_id == employee_business_id

@bp.before_request
def _enforce_staff_job_scope():
    if not _is_authenticated_employee():
        return None

    view_args = request.view_args or {}
    job_id = str(view_args.get("jobId") or "").strip()
    if not job_id or not ObjectId.is_valid(job_id):
        return None

    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": ObjectId(job_id)}, {"business_id": 1})
    if not job:
        return None

    if _employee_has_access_to_job(db, job):
        return None

    current_app.logger.warning(
        "Blocked cross-business job access: employee_id=%s job_id=%s",
        str(session.get("employee_id") or ""),
        job_id,
    )
    return redirect(url_for("jobs.jobs"))


def _extract_client_ip():
    forwarded_for = str(request.headers.get("X-Forwarded-For") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return str(request.remote_addr or "").strip()


def _notification_base_url():
    configured_base_url = get_notification_base_url()
    if configured_base_url:
        return configured_base_url

    server_name = str(current_app.config.get("SERVER_NAME") or "").strip()
    if not server_name:
        return ""

    preferred_scheme = str(current_app.config.get("PREFERRED_URL_SCHEME") or "https").strip() or "https"
    return f"{preferred_scheme}://{server_name}"


def _build_notification_url(endpoint, external=False, **route_kwargs):
    base_url = _notification_base_url()

    if base_url:
        # Build URLs inside a temporary request context so scheduler/background
        # execution does not depend on SERVER_NAME.
        with current_app.test_request_context(base_url=f"{base_url}/"):
            if external:
                return url_for(endpoint, _external=True, **route_kwargs)
            return url_for(endpoint, _external=False, **route_kwargs)

    if not external:
        return url_for(endpoint, _external=False, **route_kwargs)

    try:
        return url_for(endpoint, _external=True, **route_kwargs)
    except RuntimeError:
        current_app.logger.warning(
            "Notification URL build failed without NOTIFICATION_BASE_URL or SERVER_NAME; returning empty URL"
        )
        return ""


def _build_estimate_view_url(estimate_id, access_token=None, external=False):
    token_value = str(access_token or "").strip()
    route_kwargs = {"estimateId": estimate_id}
    if token_value:
        route_kwargs["token"] = token_value
    return _build_notification_url("jobs.view_estimate", external=external, **route_kwargs)


def _build_invoice_view_url(job_id, invoice_ref, access_token=None, external=False):
    token_value = str(access_token or "").strip()
    route_kwargs = {
        "jobId": job_id,
        "invoiceRef": invoice_ref,
    }
    if token_value:
        route_kwargs["token"] = token_value
    return _build_notification_url("jobs.view_invoice", external=external, **route_kwargs)


def _display_payment_link(payment_url):
    return str(payment_url or "").replace("https://", "").replace("http://", "")


def _issue_estimate_access_token(db, estimate_id, recipient_email=""):
    token_value = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
    db.estimates.update_one(
        {"_id": ObjectId(estimate_id)},
        {
            "$set": {
                "access_token_hash": token_hash,
                "access_token_created_at": datetime.now(UTC),
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


def _build_job_invoice_number(job_id):
    return f"INV-{str(job_id or '')[:8].upper()}"


def _build_provisional_invoice_entry(job_id):
    return {
        "invoice_id": str(ObjectId()),
        "invoice_number": _build_job_invoice_number(job_id),
        "job_id": str(job_id or "").strip(),
        "file_path": "",
        "sent_at": None,
        "due_date": "",
        "status": "Created",
        "is_provisional": True,
    }


def _ensure_job_invoice_entry(db, job_id):
    if not job_id or not ObjectId.is_valid(str(job_id)):
        return None

    job = db.jobs.find_one({"_id": ObjectId(str(job_id))})
    if not job:
        return None

    invoices = [entry for entry in (job.get("invoices") or []) if isinstance(entry, dict)]
    if invoices:
        return invoices[-1]

    invoice_entry = _build_provisional_invoice_entry(str(job_id))
    db.jobs.update_one(
        {"_id": ObjectId(str(job_id))},
        {
            "$set": {
                "invoices": [invoice_entry],
                "updated_at": datetime.now(UTC),
            }
        },
    )
    return invoice_entry


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
        entry_file_path = str(entry.get("file_path") or "").strip()
        entry_file_name = entry_file_path.split("/")[-1] if entry_file_path else ""
        target_invoice_id = str((invoice_entry or {}).get("invoice_id") or "").strip()
        target_invoice_number = str((invoice_entry or {}).get("invoice_number") or "").strip()
        target_file_path = str((invoice_entry or {}).get("file_path") or "").strip()
        target_file_name = target_file_path.split("/")[-1] if target_file_path else ""
        
        is_match = (
            (normalized_invoice_ref and entry_invoice_id == normalized_invoice_ref) or
            (normalized_invoice_ref and entry_invoice_number == normalized_invoice_ref) or
            (normalized_invoice_ref and entry_file_name == normalized_invoice_ref.split("/")[-1]) or
            (entry_invoice_id == target_invoice_id and target_invoice_id) or
            (entry_invoice_number == target_invoice_number and target_invoice_number) or
            (entry_file_name == target_file_name and target_file_name)
        )
        
        if is_match:
            updated = dict(entry)
            updated["access_token_hash"] = token_hash
            updated["access_token_created_at"] = datetime.now(UTC).isoformat()
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


INVOICE_REMINDER_DAY_OFFSETS = (
    (1, 3),
    (2, 7),
    (3, 14),
)

_invoice_reminder_indexes_ready = False


def _coerce_datetime_utc(value):
    if isinstance(value, datetime):
        return value

    raw_value = str(value or "").strip()
    if not raw_value:
        return None

    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw_value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ensure_invoice_reminder_indexes(db):
    global _invoice_reminder_indexes_ready
    if _invoice_reminder_indexes_ready:
        return

    db.invoice_reminders.create_index([("status", 1), ("scheduled_for", 1)])
    db.invoice_reminders.create_index([("job_id", 1), ("invoice_ref", 1), ("reminder_type", 1)])
    db.invoice_reminders.create_index([("business_id", 1), ("created_at", -1)])
    _invoice_reminder_indexes_ready = True


def _is_invoice_paid(job_doc, invoice_entry):
    invoice_status = str((invoice_entry or {}).get("status") or "").strip().lower()
    if invoice_status == "paid":
        return True

    job_status = str((job_doc or {}).get("status") or "").strip().lower()
    if job_status == "paid":
        return True

    balance_due = _safe_float((job_doc or {}).get("balance_due"), _safe_float((job_doc or {}).get("total_amount"), 0.0))
    return round(balance_due, 2) <= 0


def _resolve_invoice_ref(invoice_entry, fallback_ref=""):
    return str((invoice_entry or {}).get("invoice_id") or (invoice_entry or {}).get("invoice_number") or fallback_ref or "").strip()


def _resolve_company_name(business_doc):
    return str((business_doc or {}).get("company_name") or (business_doc or {}).get("business_name") or (business_doc or {}).get("name") or "Klovent").strip()


def _resolve_company_phone(business_doc):
    return str((business_doc or {}).get("phone_number") or (business_doc or {}).get("twilio_phone_number") or "").strip()


def _resolve_customer_first_name(customer_doc, job_doc):
    first_name = str((customer_doc or {}).get("first_name") or "").strip()
    if first_name:
        return first_name

    customer_name = str((job_doc or {}).get("customer_name") or "Customer").strip()
    return customer_name.split(" ", 1)[0] if customer_name else "Customer"


def _latest_invoice_for_job(job_doc):
    invoices = [entry for entry in (job_doc or {}).get("invoices") or [] if isinstance(entry, dict)]
    if not invoices:
        return None
    return invoices[-1]


def _build_payment_schedule_request_message(stage_name, first_name, amount_display, company_name, payment_link):
    return (
        f"Hi {first_name}, your {stage_name} of {amount_display} for your {company_name} service is now due. "
        f"Pay here: {payment_link}. Reply STOP to unsubscribe."
    )


def _send_payment_schedule_stage_request(db, job_id, stage_id, force_resend=False):
    if not job_id or not ObjectId.is_valid(job_id):
        return False, "Job not found"

    job = db.jobs.find_one({"_id": ObjectId(job_id)})
    if not job:
        return False, "Job not found"

    job_doc = serialize_doc(job)
    payment_schedule = list(job_doc.get("payment_schedule") or [])
    if not payment_schedule:
        return False, "Job has no payment schedule"

    target_index = -1
    target_stage = None
    for index, stage in enumerate(payment_schedule):
        if not isinstance(stage, dict):
            continue
        if str(stage.get("stage_id") or "").strip() == str(stage_id or "").strip():
            target_index = index
            target_stage = dict(stage)
            break

    if target_index < 0 or not target_stage:
        return False, "Payment stage not found"

    stage_status = str(target_stage.get("status") or "pending").strip().lower()
    if stage_status in {"paid", "cancelled"}:
        return False, "This payment stage is no longer collectible"

    if target_stage.get("request_sent_at") and not force_resend:
        return False, "Payment request already sent"

    invoice_entry = _latest_invoice_for_job(job_doc)
    if not invoice_entry:
        invoice_entry = _ensure_job_invoice_entry(db, job_id)
        job = db.jobs.find_one({"_id": ObjectId(job_id)})
        job_doc = serialize_doc(job) if job else job_doc
    if not invoice_entry:
        return False, "No invoice exists for this job yet"

    invoice_ref = _resolve_invoice_ref(invoice_entry)
    if not invoice_ref:
        return False, "Invoice reference is unavailable"

    customer = db.customers.find_one(build_reference_filter("_id", job_doc.get("customer_id"))) or {}
    business = _resolve_business_doc_for_job(db, job_doc) or {}
    channel_payload = _resolve_reminder_channel(customer, business)
    if channel_payload["channel"] == "none":
        return False, "Customer has no email or SMS destination"

    customer_email = channel_payload["email"]
    token = _issue_invoice_access_token(db, job_id, invoice_ref, customer_email)
    payment_link = _build_invoice_view_url(job_id, invoice_ref, access_token=token, external=True) if token else _build_invoice_view_url(job_id, invoice_ref, external=True)
    if not payment_link:
        return False, "Payment link could not be created"

    first_name = _resolve_customer_first_name(customer, job_doc)
    company_name = _resolve_company_name(business)
    stage_name = str(target_stage.get("name") or "Payment").strip() or "Payment"
    amount_display = normalize_currency(_safe_float(target_stage.get("amount_remaining"), target_stage.get("amount")))
    message_body = _build_payment_schedule_request_message(stage_name, first_name, amount_display, company_name, payment_link)

    email_sent = False
    sms_sent = False
    failure_reasons = []

    if channel_payload["channel"] in {"both", "email"}:
        try:
            invoice_number = str((invoice_entry or {}).get("invoice_number") or invoice_ref).strip()
            send_email(
                subject=f"Payment Request - {stage_name} - {invoice_number}",
                recipients=[customer_email],
                body=message_body,
                business=business,
            )
            email_sent = True
        except Exception as exc:
            failure_reasons.append(f"Email failed: {exc}")

    if channel_payload["channel"] in {"both", "sms"}:
        sms_ok, sms_detail = send_sms_via_twilio(
            to_number=channel_payload["to_phone"],
            from_number=channel_payload["from_phone"],
            message_body=message_body,
        )
        if sms_ok:
            sms_sent = True
        else:
            failure_reasons.append(f"SMS failed: {sms_detail}")

    if not (email_sent or sms_sent):
        return False, " | ".join(failure_reasons) or "Payment request failed"

    now_utc = datetime.now(UTC)
    target_stage["request_sent_at"] = now_utc
    if str(target_stage.get("status") or "pending").strip().lower() == "pending":
        target_stage["status"] = "due"
        target_stage["due_at"] = target_stage.get("due_at") or now_utc
    payment_schedule[target_index] = target_stage

    db.jobs.update_one(
        {"_id": ObjectId(job_id)},
        {"$set": {"payment_schedule": payment_schedule, "updated_at": now_utc}},
    )
    return True, "Payment request sent"


def _customer_is_sms_opted_out(customer_doc):
    customer = customer_doc or {}
    bool_fields = (
        "sms_opt_out",
        "sms_opted_out",
        "opt_out_sms",
        "do_not_sms",
        "do_not_text",
    )
    for field_name in bool_fields:
        if customer.get(field_name) is True:
            return True

    text_fields = (
        "sms_subscription_status",
        "sms_status",
        "sms_opt_status",
    )
    for field_name in text_fields:
        normalized = str(customer.get(field_name) or "").strip().lower()
        if normalized in {"opted_out", "unsubscribed", "stop", "stopped"}:
            return True

    return False


def _build_invoice_reminder_message(reminder_type, reminder_number, first_name, total_display, company_name, payment_link, company_phone):
    if reminder_type == "manual":
        return (
            f"Hi {first_name}, friendly reminder your invoice of {total_display} from {company_name} is still outstanding. "
            f"Pay here: {payment_link} or call {company_phone}."
        )

    if reminder_number == 1:
        return (
            f"Hi {first_name}, friendly reminder your invoice of {total_display} from {company_name} is outstanding. "
            f"Pay here: {payment_link}. Reply STOP to unsubscribe."
        )

    if reminder_number == 2:
        return (
            f"Hi {first_name}, your invoice of {total_display} from {company_name} is 7 days past due. "
            f"Pay here: {payment_link} or call {company_phone}."
        )

    if reminder_number == 3:
        return (
            f"Hi {first_name}, final notice - your invoice of {total_display} from {company_name} remains unpaid. "
            f"Pay here: {payment_link} or call {company_phone}."
        )

    return (
        f"Hi {first_name}, this is a reminder your invoice of {total_display} from {company_name} is still unpaid. "
        f"Pay here: {payment_link} or call {company_phone}."
    )


def _resolve_reminder_channel(customer_doc, business_doc):
    customer_email = str((customer_doc or {}).get("email") or "").strip()
    customer_phone = normalize_phone_for_twilio((customer_doc or {}).get("phone"))
    twilio_phone = normalize_phone_for_twilio((business_doc or {}).get("twilio_phone_number"))

    sms_allowed = (
        sms_features_enabled()
        and not _customer_is_sms_opted_out(customer_doc)
        and bool(customer_phone)
        and bool(twilio_phone)
    )
    email_allowed = bool(customer_email)

    if sms_allowed and email_allowed:
        channel = "both"
    elif sms_allowed:
        channel = "sms"
    elif email_allowed:
        channel = "email"
    else:
        channel = "none"

    return {
        "channel": channel,
        "email": customer_email,
        "to_phone": customer_phone,
        "from_phone": twilio_phone,
    }


def _cancel_pending_invoice_reminders(db, job_id, invoice_ref, reason="Invoice paid"):
    now_utc = datetime.now(UTC)
    result = db.invoice_reminders.update_many(
        {
            "job_id": str(job_id or "").strip(),
            "invoice_ref": str(invoice_ref or "").strip(),
            "status": "scheduled",
            "reminder_type": "automatic",
        },
        {
            "$set": {
                "status": "cancelled",
                "cancelled_at": now_utc,
                "updated_at": now_utc,
                "error_message": str(reason or "").strip(),
            }
        },
    )
    return int(result.modified_count or 0)


def schedule_invoice_reminders_for_invoice(db, job_doc, invoice_entry, sent_at=None):
    if not job_doc or not invoice_entry:
        return 0

    _ensure_invoice_reminder_indexes(db)
    scheduled_base = sent_at if isinstance(sent_at, datetime) else datetime.now(UTC)

    job_id = str((job_doc or {}).get("_id") or "").strip()
    invoice_ref = _resolve_invoice_ref(invoice_entry)
    if not job_id or not invoice_ref:
        return 0

    _cancel_pending_invoice_reminders(db, job_id, invoice_ref, reason="Invoice resent - reminder schedule refreshed")

    customer_id = str((job_doc or {}).get("customer_id") or "").strip()
    business_id = str((job_doc or {}).get("business_id") or "").strip()
    now_utc = datetime.now(UTC)
    docs = []

    for reminder_number, day_offset in INVOICE_REMINDER_DAY_OFFSETS:
        docs.append(
            {
                "job_id": job_id,
                "invoice_ref": invoice_ref,
                "invoice_number": str((invoice_entry or {}).get("invoice_number") or "").strip(),
                "invoice_id": str((invoice_entry or {}).get("invoice_id") or "").strip(),
                "business_id": business_id,
                "customer_id": customer_id,
                "reminder_type": "automatic",
                "automatic_sequence_number": reminder_number,
                "scheduled_for": scheduled_base + timedelta(days=day_offset),
                "sent_at": None,
                "automatic_sent_at": None,
                "status": "scheduled",
                "channel": "both",
                "error_message": "",
                "created_at": now_utc,
                "updated_at": now_utc,
            }
        )

    if not docs:
        return 0

    result = db.invoice_reminders.insert_many(docs)
    return len(list(getattr(result, "inserted_ids", []) or []))


def _send_single_invoice_reminder(db, reminder_doc):
    reminder_id = reminder_doc.get("_id")
    now_utc = datetime.now(UTC)

    job_id = str((reminder_doc or {}).get("job_id") or "").strip()
    invoice_ref = str((reminder_doc or {}).get("invoice_ref") or "").strip()
    reminder_type = str((reminder_doc or {}).get("reminder_type") or "automatic").strip().lower()
    reminder_number = int((reminder_doc or {}).get("automatic_sequence_number") or 0)

    if not job_id or not ObjectId.is_valid(job_id) or not invoice_ref:
        db.invoice_reminders.update_one(
            {"_id": reminder_id},
            {"$set": {"status": "failed", "error_message": "Missing invoice reference context", "updated_at": now_utc}},
        )
        return False

    job = db.jobs.find_one({"_id": ObjectId(job_id)})
    if not job:
        db.invoice_reminders.update_one(
            {"_id": reminder_id},
            {"$set": {"status": "failed", "error_message": "Job not found", "updated_at": now_utc}},
        )
        return False

    job_doc = serialize_doc(job)
    invoice_entry = _find_invoice_entry(job_doc, invoice_ref=invoice_ref)
    if not invoice_entry:
        db.invoice_reminders.update_one(
            {"_id": reminder_id},
            {"$set": {"status": "failed", "error_message": "Invoice not found", "updated_at": now_utc}},
        )
        return False

    if _is_invoice_paid(job_doc, invoice_entry):
        db.invoice_reminders.update_one(
            {"_id": reminder_id},
            {
                "$set": {
                    "status": "cancelled",
                    "error_message": "Invoice already paid",
                    "cancelled_at": now_utc,
                    "updated_at": now_utc,
                }
            },
        )
        return False

    customer = db.customers.find_one(build_reference_filter("_id", (job_doc or {}).get("customer_id"))) or {}
    business = _resolve_business_doc_for_job(db, job_doc) or {}

    channel_payload = _resolve_reminder_channel(customer, business)
    if channel_payload["channel"] == "none":
        db.invoice_reminders.update_one(
            {"_id": reminder_id},
            {
                "$set": {
                    "status": "cancelled",
                    "channel": "none",
                    "error_message": "Customer has no email or SMS destination",
                    "cancelled_at": now_utc,
                    "updated_at": now_utc,
                }
            },
        )
        current_app.logger.warning("Invoice reminder cancelled: missing contact channels job_id=%s invoice_ref=%s", job_id, invoice_ref)
        return False

    customer_email = channel_payload["email"]
    token = _issue_invoice_access_token(db, job_id, invoice_ref, customer_email)
    payment_link = _build_invoice_view_url(job_id, invoice_ref, access_token=token, external=True) if token else _build_invoice_view_url(job_id, invoice_ref, external=True)

    first_name = _resolve_customer_first_name(customer, job_doc)
    total_display = normalize_currency(str((job_doc or {}).get("total_amount") or 0.0))
    company_name = _resolve_company_name(business)
    company_phone = _resolve_company_phone(business) or "our office"

    message_body = _build_invoice_reminder_message(
        reminder_type,
        reminder_number,
        first_name,
        total_display,
        company_name,
        payment_link,
        company_phone,
    )

    email_sent = False
    sms_sent = False
    failure_reasons = []

    if channel_payload["channel"] in {"both", "email"}:
        try:
            email_subject = f"Invoice Reminder - {str((invoice_entry or {}).get('invoice_number') or invoice_ref)}"
            if reminder_number == 3:
                email_subject = f"Final Notice - {str((invoice_entry or {}).get('invoice_number') or invoice_ref)}"
            if reminder_type == "manual":
                email_subject = f"Manual Payment Reminder - {str((invoice_entry or {}).get('invoice_number') or invoice_ref)}"

            send_email(subject=email_subject, recipients=[customer_email], body=message_body, business=business)
            email_sent = True
        except Exception as exc:
            failure_reasons.append(f"Email failed: {exc}")

    if channel_payload["channel"] in {"both", "sms"}:
        sms_ok, sms_detail = send_sms_via_twilio(
            to_number=channel_payload["to_phone"],
            from_number=channel_payload["from_phone"],
            message_body=message_body,
        )
        if sms_ok:
            sms_sent = True
        else:
            failure_reasons.append(f"SMS failed: {sms_detail}")

    if email_sent and sms_sent:
        resolved_channel = "both"
    elif email_sent:
        resolved_channel = "email"
    elif sms_sent:
        resolved_channel = "sms"
    else:
        resolved_channel = channel_payload["channel"]

    status = "sent" if (email_sent or sms_sent) else "failed"
    error_text = " | ".join(failure_reasons)

    db.invoice_reminders.update_one(
        {"_id": reminder_id},
        {
            "$set": {
                "status": status,
                "channel": resolved_channel,
                "sent_at": now_utc,
                "manual_sent_at": now_utc if reminder_type == "manual" else reminder_doc.get("manual_sent_at"),
                "automatic_sent_at": now_utc if reminder_type == "automatic" else reminder_doc.get("automatic_sent_at"),
                "updated_at": now_utc,
                "error_message": error_text,
            }
        },
    )
    return status == "sent"


def process_due_invoice_reminders(db=None, batch_size=100):
    if db is None:
        db = ensure_connection_or_500()
    _ensure_invoice_reminder_indexes(db)

    due_now = datetime.now(UTC)
    reminders = list(
        db.invoice_reminders.aggregate(
            [
                {
                    "$match": {
                        "status": "scheduled",
                        "reminder_type": "automatic",
                    }
                },
                {
                    "$addFields": {
                        "scheduled_for_utc": {
                            "$convert": {
                                "input": "$scheduled_for",
                                "to": "date",
                                "onError": None,
                                "onNull": None,
                            }
                        }
                    }
                },
                {
                    "$match": {
                        "scheduled_for_utc": {"$ne": None, "$lte": due_now},
                    }
                },
                {"$sort": {"scheduled_for_utc": 1}},
                {"$limit": max(1, int(batch_size or 100))},
            ]
        )
    )

    processed = 0
    for reminder in reminders:
        try:
            _send_single_invoice_reminder(db, reminder)
        except Exception as exc:
            db.invoice_reminders.update_one(
                {"_id": reminder.get("_id")},
                {
                    "$set": {
                        "status": "failed",
                        "error_message": str(exc),
                        "updated_at": datetime.now(UTC),
                    }
                },
            )
            current_app.logger.error("Invoice reminder processing failed: reminder_id=%s error=%s", str(reminder.get("_id") or ""), exc)
        finally:
            processed += 1

    return processed


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


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _reference_match(field_name, value, extra_match=None):
    clauses = [build_reference_filter(field_name, value)]
    if extra_match:
        clauses.append(extra_match)
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _reference_storage_value(value):
    oid_value = coerce_object_id(value)
    if oid_value is not None:
        return oid_value
    return str(value or "").strip()


def _normalize_payment_method(value):
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"card", "ach", "cash", "check"} else ""


def _payment_method_from_checkout_session(checkout_session):
    payment_method_types = _stripe_obj_value(checkout_session, "payment_method_types", []) or []
    if not isinstance(payment_method_types, list):
        payment_method_types = []
    normalized_types = {str(item or "").strip().lower() for item in payment_method_types}
    if "us_bank_account" in normalized_types:
        return "ach"
    return "card"


def _normalize_stripe_payment_method_type(method_type):
    normalized = str(method_type or "").strip().lower()
    if normalized == "us_bank_account":
        return "ach"
    if normalized == "card":
        return "card"
    return ""


def _resolve_payment_method_from_payment_intent(payment_intent_id):
    intent_id = str(payment_intent_id or "").strip()
    if not intent_id or not _configure_stripe_client():
        return ""

    try:
        payment_intent = stripe.PaymentIntent.retrieve(
            intent_id,
            expand=["payment_method", "latest_charge"],
        )
    except Exception as exc:
        current_app.logger.warning("Failed to retrieve payment intent for method resolution: payment_intent=%s error=%s", intent_id, exc)
        return ""

    payment_method_obj = _stripe_obj_value(payment_intent, "payment_method", None)
    payment_method_type = _stripe_obj_value(payment_method_obj, "type", "")

    # Some API responses return only a PaymentMethod ID on the intent.
    if not payment_method_type and isinstance(payment_method_obj, str):
        method_id = str(payment_method_obj or "").strip()
        if method_id:
            try:
                payment_method = stripe.PaymentMethod.retrieve(method_id)
                payment_method_type = _stripe_obj_value(payment_method, "type", "")
            except Exception as exc:
                current_app.logger.warning("Failed to retrieve payment method for method resolution: payment_method=%s error=%s", method_id, exc)

    normalized = _normalize_stripe_payment_method_type(payment_method_type)
    if normalized:
        return normalized

    latest_charge = _stripe_obj_value(payment_intent, "latest_charge", None)
    payment_method_details = _stripe_obj_value(latest_charge, "payment_method_details", None)
    charge_method_type = _stripe_obj_value(payment_method_details, "type", "")
    return _normalize_stripe_payment_method_type(charge_method_type)


def _first_completed_payment_datetime(payments):
    earliest_value = None
    for payment in payments:
        if not isinstance(payment, dict):
            continue
        paid_at_value = payment.get("paid_at")
        parsed_value = _coerce_datetime_utc(paid_at_value)
        if parsed_value is None:
            continue
        if earliest_value is None or parsed_value < earliest_value:
            earliest_value = parsed_value
    return earliest_value


def _refresh_customer_balance_from_jobs(db, customer_id):
    if not customer_id:
        return
    balance_due_total = 0.0
    for job_doc in db.jobs.find(build_reference_filter("customer_id", customer_id), {"balance_due": 1, "total_amount": 1, "total_amount_paid": 1}):
        if "balance_due" in job_doc:
            balance_due_total += max(0.0, _safe_float(job_doc.get("balance_due"), 0.0))
            continue
        total_amount = _safe_float(job_doc.get("total_amount"), 0.0)
        total_amount_paid = _safe_float(job_doc.get("total_amount_paid"), 0.0)
        balance_due_total += max(0.0, total_amount - total_amount_paid)

    customer = db.customers.find_one(build_reference_filter("_id", customer_id), {"_id": 1})
    if not customer:
        return

    rounded_balance = round(balance_due_total, 2)
    db.customers.update_one(
        {"_id": customer.get("_id")},
        {
            "$set": {
                "balance_due_amount": rounded_balance,
                "balance_due": normalize_currency(rounded_balance),
            }
        },
    )


def _synchronize_job_payment_fields(db, job_id):
    job = db.jobs.find_one({"_id": ObjectId(job_id)})
    if not job:
        return None

    payment_state = _calculate_job_payment_state(db, job)
    total_amount = payment_state["total_due"]
    payment_docs = list(
        db.payments.find(
            _reference_match(
                "job_id",
                job_id,
                {"status": "completed"},
            )
        )
    )
    total_amount_paid = payment_state["amount_paid"]
    balance_due = payment_state["balance_due"]

    if balance_due <= 0:
        payment_status = "paid"
    elif total_amount_paid <= 0:
        payment_status = "pending_paid"
    else:
        payment_status = "partial_paid"

    first_paid_at = _first_completed_payment_datetime(payment_docs)
    existing_paid_at = _coerce_datetime_utc(job.get("paid_at"))
    paid_at_value = existing_paid_at or first_paid_at

    job_updates = {
        "total_amount_paid": total_amount_paid,
        "balance_due": balance_due,
        "payment_status": payment_status,
        "updated_at": datetime.now(UTC),
    }
    if paid_at_value is not None:
        job_updates["paid_at"] = paid_at_value
        job_updates["datePaid"] = paid_at_value.strftime("%m/%d/%Y %H:%M:%S")

    has_completed_payment = total_amount_paid > 0 or first_paid_at is not None
    current_status_normalized = str(job.get("status") or "").strip().lower()
    has_completed_marker = bool(job.get("completed_at")) or bool(str(job.get("dateCompleted") or "").strip())
    has_invoice_entry = any(isinstance(entry, dict) for entry in (job.get("invoices") or []))
    has_free_completion_eligibility = has_completed_marker and has_invoice_entry

    if balance_due <= 0 and (has_completed_payment or has_free_completion_eligibility):
        job_updates["status"] = "Paid"
    elif balance_due <= 0 and current_status_normalized == "paid":
        # Recover legacy free jobs that were auto-marked Paid without an actual payment.
        has_started_marker = bool(job.get("started_at")) or bool(str(job.get("dateStarted") or "").strip())
        has_en_route_marker = bool(job.get("en_route_at"))
        has_schedule = bool(str(job.get("scheduled_date") or "").strip()) and bool(str(job.get("scheduled_time") or "").strip())

        if has_completed_marker:
            job_updates["status"] = "Completed"
        elif has_started_marker:
            job_updates["status"] = "Started"
        elif has_en_route_marker:
            job_updates["status"] = "En Route"
        elif has_schedule:
            job_updates["status"] = "Scheduled"
        else:
            job_updates["status"] = "Pending"

    db.jobs.update_one({"_id": ObjectId(job_id)}, {"$set": job_updates})

    if job.get("payment_schedule"):
        _sync_job_payment_schedule(db, job_id)

    refreshed = db.jobs.find_one({"_id": ObjectId(job_id)})
    if refreshed:
        _refresh_customer_balance_from_jobs(db, refreshed.get("customer_id"))
    return refreshed


def _sanitize_invoice_payment_fields(invoice_entry):
    sanitized = dict(invoice_entry or {})
    for deprecated_field in (
        "payment_status",
        "amount_paid",
        "payment_type",
        "payment_notes",
        "check_number",
        "stripe_payment_intent_id",
        "stripe_checkout_session_id",
        "paid_at",
    ):
        sanitized.pop(deprecated_field, None)
    return sanitized


def _update_invoice_status_from_job_payment(db, job_doc, invoice_ref):
    if not job_doc:
        return

    normalized_invoice_ref = str(invoice_ref or "").strip()
    balance_due = round(_safe_float(job_doc.get("balance_due"), _safe_float(job_doc.get("total_amount"), 0.0)), 2)
    is_fully_paid = balance_due <= 0

    updated_invoices = []
    for entry in list(job_doc.get("invoices") or []):
        if not isinstance(entry, dict):
            updated_invoices.append(entry)
            continue

        updated_entry = _sanitize_invoice_payment_fields(entry)
        entry_invoice_id = str(updated_entry.get("invoice_id") or "").strip()
        entry_invoice_number = str(updated_entry.get("invoice_number") or "").strip()
        if normalized_invoice_ref and normalized_invoice_ref in {entry_invoice_id, entry_invoice_number}:
            updated_entry["status"] = "Paid" if is_fully_paid else "Sent"
        updated_invoices.append(updated_entry)

    db.jobs.update_one(
        {"_id": job_doc.get("_id")},
        {
            "$set": {
                "invoices": updated_invoices,
                "updated_at": datetime.now(UTC),
            }
        },
    )


def _record_payment(
    db,
    job_id,
    invoice_ref,
    amount,
    payment_method,
    status="completed",
    stripe_payment_intent_id="",
    check_number="",
    notes="",
):
    if not ObjectId.is_valid(job_id):
        return False

    job = db.jobs.find_one({"_id": ObjectId(job_id)})
    if not job:
        return False

    job_doc = serialize_doc(job)
    invoice_entry = _find_invoice_entry(job_doc, invoice_ref=invoice_ref)
    if not invoice_entry:
        return False

    normalized_method = _normalize_payment_method(payment_method)
    if not normalized_method:
        return False

    payment_amount = round(_safe_float(amount, 0.0), 2)
    if payment_amount <= 0:
        return False

    payment_state = _calculate_job_payment_state(db, job_doc)
    current_balance_due = payment_state["balance_due"]
    if payment_amount > current_balance_due:
        return False

    paid_at_utc = datetime.now(UTC)
    recorded_by = str(session.get("employee_id") or "").strip() if has_request_context() else ""
    payment_doc = {
        "job_id": _reference_storage_value(job_id),
        "invoice_id": str((invoice_entry or {}).get("invoice_id") or (invoice_entry or {}).get("invoice_number") or "").strip(),
        "company_id": _reference_storage_value(job.get("business_id")),
        "customer_id": _reference_storage_value(job.get("customer_id")),
        "amount": payment_amount,
        "payment_method": normalized_method,
        "stripe_payment_intent_id": str(stripe_payment_intent_id or "").strip() if normalized_method in {"card", "ach"} else None,
        "check_number": str(check_number or "").strip() if normalized_method == "check" else None,
        "status": str(status or "completed").strip().lower() or "completed",
        "paid_at": paid_at_utc,
        "recorded_by": _reference_storage_value(recorded_by),
        "notes": str(notes or "").strip(),
        "quickbooks_payment_id": None,
        "synced_to_quickbooks_at": None,
        "created_at": paid_at_utc,
    }
    db.payments.insert_one(payment_doc)

    refreshed_job = _synchronize_job_payment_fields(db, job_id)
    if refreshed_job:
        _update_invoice_status_from_job_payment(db, refreshed_job, invoice_ref)
        refreshed_job_doc = serialize_doc(refreshed_job)
        refreshed_invoice = _find_invoice_entry(refreshed_job_doc, invoice_ref=invoice_ref)
        if refreshed_invoice and str(refreshed_invoice.get("status") or "").strip().lower() == "paid":
            _cancel_pending_invoice_reminders(
                db,
                job_id,
                _resolve_invoice_ref(refreshed_invoice, fallback_ref=invoice_ref),
                reason="Invoice marked paid",
            )

    return True


def process_stripe_checkout_completed(db, checkout_session):
    metadata_raw = _stripe_obj_value(checkout_session, "metadata", {}) or {}
    metadata = _stripe_obj_dict(metadata_raw)
    job_id = str(metadata.get("job_id") or "").strip()
    invoice_ref = str(
        metadata.get("invoice_ref")
        or metadata.get("invoice_id")
        or metadata.get("invoice_number")
        or ""
    ).strip()

    if not job_id or not invoice_ref:
        client_reference_id = str(_stripe_obj_value(checkout_session, "client_reference_id", "") or "").strip()
        if ":" in client_reference_id:
            parsed_job_id, parsed_invoice_ref = client_reference_id.split(":", 1)
            if not job_id:
                job_id = str(parsed_job_id or "").strip()
            if not invoice_ref:
                invoice_ref = str(parsed_invoice_ref or "").strip()

    if (not job_id or not invoice_ref) and str(_stripe_obj_value(checkout_session, "payment_intent", "") or "").strip() and _configure_stripe_client():
        try:
            payment_intent = stripe.PaymentIntent.retrieve(str(_stripe_obj_value(checkout_session, "payment_intent", "") or "").strip())
            payment_intent_metadata = _stripe_obj_dict(_stripe_obj_value(payment_intent, "metadata", {}) or {})
            if not job_id:
                job_id = str(payment_intent_metadata.get("job_id") or "").strip()
            if not invoice_ref:
                invoice_ref = str(
                    payment_intent_metadata.get("invoice_ref")
                    or payment_intent_metadata.get("invoice_id")
                    or payment_intent_metadata.get("invoice_number")
                    or ""
                ).strip()
        except Exception as exc:
            current_app.logger.warning(
                "Stripe checkout completion could not resolve payment intent metadata: error=%s",
                exc,
            )

    if not job_id or not invoice_ref or not ObjectId.is_valid(job_id):
        current_app.logger.warning(
            "Stripe checkout completion ignored due to missing metadata: job_id=%s invoice_ref=%s",
            job_id,
            invoice_ref,
        )
        return False

    amount_total = float(_stripe_obj_value(checkout_session, "amount_total", 0) or 0) / 100.0
    if amount_total <= 0:
        amount_total = _safe_float(metadata.get("amount"), 0.0)

    # Validate payment amount against metadata (what was requested)
    expected_amount = _safe_float(metadata.get("amount"), 0.0)
    if expected_amount > 0 and round(amount_total, 2) != round(expected_amount, 2):
        current_app.logger.warning(
            "Stripe checkout amount mismatch: job_id=%s invoice_ref=%s expected=%.2f actual=%.2f",
            job_id,
            invoice_ref,
            expected_amount,
            amount_total,
        )
        return False

    # Validate amount doesn't exceed current balance_due (prevent overpayment tampering)
    job = db.jobs.find_one({"_id": ObjectId(job_id)}) if ObjectId.is_valid(job_id) else None
    if job:
        job_doc = serialize_doc(job)
        payment_state = _calculate_job_payment_state(db, job_doc)
        current_balance_due = payment_state.get("balance_due", 0.0)
        
        if amount_total > round(current_balance_due, 2):
            current_app.logger.warning(
                "Stripe checkout amount exceeds current balance: job_id=%s invoice_ref=%s amount=%.2f balance_due=%.2f",
                job_id,
                invoice_ref,
                amount_total,
                current_balance_due,
            )
            return False

    payment_intent_id = str(_stripe_obj_value(checkout_session, "payment_intent", "") or "").strip()
    payment_method = _resolve_payment_method_from_payment_intent(payment_intent_id) or _payment_method_from_checkout_session(checkout_session)
    finalized = _record_payment(
        db,
        job_id,
        invoice_ref,
        amount=amount_total,
        payment_method=payment_method,
        stripe_payment_intent_id=payment_intent_id,
        status="completed",
    )
    if not finalized:
        current_app.logger.warning(
            "Stripe checkout completion failed to record payment: job_id=%s invoice_ref=%s amount=%s payment_intent=%s metadata=%s",
            job_id,
            invoice_ref,
            amount_total,
            payment_intent_id,
            metadata,
        )
        return False

    stripe_customer_id = str(_stripe_obj_value(checkout_session, "customer", "") or "").strip()
    if stripe_customer_id:
        job = db.jobs.find_one({"_id": ObjectId(job_id)}, {"customer_id": 1}) or {}
        customer = db.customers.find_one(build_reference_filter("_id", job.get("customer_id")), {"_id": 1, "stripe_customer_id": 1})
        if customer and not str(customer.get("stripe_customer_id") or "").strip():
            db.customers.update_one(
                {"_id": customer.get("_id")},
                {"$set": {"stripe_customer_id": stripe_customer_id}},
            )
    return True


def _normalize_manual_payment_type(value):
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"cash", "check"} else ""


@bp.route("/jobs/<jobId>/invoices/<invoiceRef>/record-payment", methods=["POST"])
def record_invoice_payment(jobId, invoiceRef):
    db = ensure_connection_or_500()
    if not _is_authenticated_employee():
        return redirect(url_for("auth.login"))

    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    job_doc = serialize_doc(job)
    invoice = _find_invoice_entry(job_doc, invoice_ref=invoiceRef)
    if not invoice:
        return redirect(url_for("jobs.view_job", jobId=jobId))

    payment_type = _normalize_manual_payment_type(request.form.get("payment_type"))
    if not payment_type:
        return redirect(url_for("jobs.view_invoice", jobId=jobId, invoiceRef=invoiceRef, payment="invalid"))

    payment_state = _calculate_job_payment_state(db, job_doc)
    balance_due = payment_state["balance_due"]

    amount_received = round(float(currency_to_float(request.form.get("amount_received", ""))), 2)
    check_number = ""
    if balance_due <= 0:
        return redirect(url_for("jobs.view_invoice", jobId=jobId, invoiceRef=invoiceRef, payment="already_paid"))

    if payment_type == "cash":
        if amount_received <= 0:
            return redirect(url_for("jobs.view_invoice", jobId=jobId, invoiceRef=invoiceRef, payment="invalid"))
    elif payment_type == "check":
        check_number = str(request.form.get("check_number") or "").strip()
        if not check_number:
            return redirect(url_for("jobs.view_invoice", jobId=jobId, invoiceRef=invoiceRef, payment="invalid"))
        if amount_received <= 0:
            amount_received = balance_due

    if amount_received > balance_due:
        return redirect(url_for("jobs.view_invoice", jobId=jobId, invoiceRef=invoiceRef, payment="invalid"))

    payment_notes = str(request.form.get("payment_notes") or "").strip()
    finalized = _record_payment(
        db,
        jobId,
        invoiceRef,
        amount=amount_received,
        payment_method=payment_type,
        status="completed",
        notes=payment_notes,
        check_number=check_number,
    )
    if not finalized:
        return redirect(url_for("jobs.view_invoice", jobId=jobId, invoiceRef=invoiceRef, payment="failed"))

    return redirect(
        url_for(
            "jobs.view_invoice",
            jobId=jobId,
            invoiceRef=invoiceRef,
            payment="success",
            payment_method=payment_type,
        )
    )


@bp.route("/jobs/<jobId>/invoices/<invoiceRef>/send-reminder", methods=["POST"])
def send_invoice_reminder_manually(jobId, invoiceRef):
    db = ensure_connection_or_500()
    if not _is_authenticated_employee():
        return redirect(url_for("auth.login"))

    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    job_doc = serialize_doc(job)
    invoice = _find_invoice_entry(job_doc, invoice_ref=invoiceRef)
    if not invoice:
        return redirect(url_for("jobs.view_job", jobId=jobId))

    if _is_invoice_paid(job_doc, invoice):
        return redirect(url_for("jobs.view_invoice", jobId=jobId, invoiceRef=invoiceRef, reminder="skipped_paid"))

    _ensure_invoice_reminder_indexes(db)
    now_utc = datetime.now(UTC)

    reminder_doc = {
        "job_id": str(jobId or "").strip(),
        "invoice_ref": _resolve_invoice_ref(invoice, fallback_ref=invoiceRef),
        "invoice_number": str((invoice or {}).get("invoice_number") or "").strip(),
        "invoice_id": str((invoice or {}).get("invoice_id") or "").strip(),
        "business_id": str((job_doc or {}).get("business_id") or "").strip(),
        "customer_id": str((job_doc or {}).get("customer_id") or "").strip(),
        "reminder_type": "manual",
        "scheduled_for": now_utc,
        "sent_at": None,
        "manual_sent_at": None,
        "status": "scheduled",
        "channel": "both",
        "error_message": "",
        "created_by_employee_id": str(session.get("employee_id") or "").strip(),
        "created_at": now_utc,
        "updated_at": now_utc,
    }
    insert_result = db.invoice_reminders.insert_one(reminder_doc)
    created = db.invoice_reminders.find_one({"_id": insert_result.inserted_id})

    sent_ok = False
    if created:
        sent_ok = _send_single_invoice_reminder(db, created)

    state = "sent" if sent_ok else "failed"
    return redirect(url_for("jobs.view_invoice", jobId=jobId, invoiceRef=invoiceRef, reminder=state))


@bp.route("/jobs/<jobId>/payment-schedule/<stageId>/send-request", methods=["POST"])
def send_payment_schedule_request(jobId, stageId):
    if not _is_authenticated_employee():
        return redirect(url_for("auth.login"))

    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    force_resend = str(request.form.get("force_resend") or "").strip().lower() in {"1", "true", "yes", "on"}
    ok, detail = _send_payment_schedule_stage_request(db, jobId, stageId, force_resend=force_resend)
    state = "sent" if ok else "failed"
    reason = detail if not ok else ""
    return redirect(url_for("jobs.view_job", jobId=jobId, payment_schedule_request=state, payment_schedule_reason=reason))


def _coerce_line_amount(value):
    return float(currency_to_float(value))


PLAN_DISCOUNT_ROW_NAME = "Maintenance Plan Savings"
PLAN_DISCOUNT_SOURCE = "maintenance_plan"


def _is_plan_discount_row(discount):
    if not isinstance(discount, dict):
        return False
    if str(discount.get("source") or "").strip() == PLAN_DISCOUNT_SOURCE:
        return True
    return str(discount.get("discount_name") or "").strip() == PLAN_DISCOUNT_ROW_NAME


def _strip_plan_discount_rows(discounts):
    return [
        discount
        for discount in (discounts or [])
        if not _is_plan_discount_row(discount)
    ]


def _calculate_discount_totals(payload, subtotal):
    discount_rows = []
    discounts_total = 0.0

    for discount in (payload.get("discounts") or []):
        if not isinstance(discount, dict):
            continue

        percent_value = _coerce_line_amount(discount.get("discount_percentage"))
        if percent_value > 0:
            amount_value = max(0.0, subtotal * (percent_value / 100.0))
        else:
            amount_value = abs(_coerce_line_amount(discount.get("discount_amount") or discount.get("line_total")))

        if amount_value <= 0:
            continue

        discounts_total += amount_value
        discount_rows.append(
            {
                "name": str(discount.get("discount_name") or "Discount").strip() or "Discount",
                "amount": amount_value,
                "is_plan_savings": _is_plan_discount_row(discount),
            }
        )

    # Cap the combined discount at the subtotal and proportionally trim the rows
    # so the itemized list still sums to the (capped) total shown to the customer.
    capped_total = min(discounts_total, max(0.0, subtotal))
    if discount_rows and capped_total < discounts_total and discounts_total > 0:
        scale = capped_total / discounts_total
        for row in discount_rows:
            row["amount"] = round(row["amount"] * scale, 2)

    return discount_rows, capped_total


def _build_pricing_summary(payload, business_doc=None, customer_doc=None):
    source = payload or {}
    customer = customer_doc or {}
    business = business_doc or {}

    def _resolve_service_amount(service):
        if not isinstance(service, dict):
            return 0
        return service.get("price") or service.get("standard_price") or 0

    def _resolve_part_amount(part):
        if not isinstance(part, dict):
            return 0
        return part.get("price") or part.get("sell_price") or part.get("unit_cost") or 0

    services_total = sum(
        _coerce_line_amount(_resolve_service_amount(service))
        for service in (source.get("services") or [])
    )
    parts_total = sum(
        _coerce_line_amount(_resolve_part_amount(part))
        for part in (source.get("parts") or [])
    )
    labors_total = sum(_coerce_line_amount(labor.get("line_total") or labor.get("hourly_rate")) for labor in (source.get("labors") or []))
    materials_total = sum(_coerce_line_amount(material.get("line_total") or material.get("price")) for material in (source.get("materials") or []))
    equipment_total = sum(_coerce_line_amount(equipment.get("line_total") or equipment.get("price")) for equipment in (source.get("equipments") or []))

    subtotal = services_total + parts_total + labors_total + materials_total + equipment_total
    discount_rows, discounts_total = _calculate_discount_totals(source, subtotal)
    pre_tax_total = max(0.0, subtotal - discounts_total)

    tax_inputs = build_line_item_tax_inputs(source)
    if subtotal > 0 and discounts_total > 0:
        discount_ratio = pre_tax_total / subtotal
        tax_inputs = [{**item, "amount": item["amount"] * discount_ratio} for item in tax_inputs]
    tax_breakdown = calculate_itemized_tax(
        tax_inputs,
        normalize_business_tax_rates(business),
        customer_tax_exempt=bool(customer.get("tax_exempt")),
    )

    total_due = round(pre_tax_total + tax_breakdown.get("tax_total", 0.0), 2)
    tax_lines = tax_breakdown.get("tax_lines") or []

    discount_lines = [
        {
            "name": row["name"],
            "amount": round(row["amount"], 2),
            "amount_display": normalize_currency(row["amount"]),
            "is_plan_savings": bool(row.get("is_plan_savings")),
        }
        for row in discount_rows
    ]

    return {
        "subtotal": round(subtotal, 2),
        "tax_total": round(tax_breakdown.get("tax_total", 0.0), 2),
        "discounts_total": round(discounts_total, 2),
        "discount_lines": discount_lines,
        "total_due": total_due,
        "tax_lines": tax_lines,
        "is_tax_exempt": bool(tax_breakdown.get("is_tax_exempt")),
        "has_taxable_items": bool(tax_breakdown.get("has_taxable_items")),
        "subtotal_display": normalize_currency(subtotal),
        "tax_total_display": normalize_currency(tax_breakdown.get("tax_total", 0.0)),
        "discounts_total_display": normalize_currency(discounts_total),
        "total_due_display": normalize_currency(total_due),
    }


def _build_estimate_pricing_summary(estimate, business_doc=None, customer_doc=None):
    return _build_pricing_summary(estimate or {}, business_doc=business_doc, customer_doc=customer_doc)


def _build_invoice_pricing_summary(job_doc, business_doc=None, customer_doc=None):
    return _build_pricing_summary(job_doc or {}, business_doc=business_doc, customer_doc=customer_doc)


def get_active_plan_for_property(db, property_id, business_id):
    normalized_property_id = str(property_id or "").strip()
    if not normalized_property_id:
        return None

    conditions = [build_reference_filter("property_id", normalized_property_id), {"status": "active"}]
    normalized_business_id = str(business_id or "").strip()
    if normalized_business_id:
        conditions.append(build_reference_filter("business_id", normalized_business_id))

    return db.maintenance_plans.find_one({"$and": conditions})


def _apply_discount_to_currency(value, discount_pct):
    base_amount = currency_to_float(value)
    discounted = base_amount * (1 - (discount_pct / 100.0))
    if discounted < 0:
        discounted = 0.0
    return normalize_currency(discounted)


def _resync_line_total_from_unit_price(line_item, *quantity_keys):
    quantity_value = 0.0
    for key in quantity_keys:
        if line_item.get(key) not in (None, ""):
            quantity_value = currency_to_float(line_item.get(key))
            break
    unit_price = currency_to_float(line_item.get("price"))
    line_total = normalize_currency(quantity_value * unit_price)
    if "unit_price" in line_item:
        line_item["unit_price"] = line_item.get("price")
    if "line_total" in line_item:
        line_item["line_total"] = line_total
    if "total" in line_item:
        line_item["total"] = line_total


def apply_plan_discount(job_document, plan, business_doc=None, customer_doc=None):
    snapshot = (plan or {}).get("template_snapshot") or {}

    try:
        discount_pct = float(snapshot.get("repair_discount_pct") or 0)
    except (TypeError, ValueError):
        discount_pct = 0.0

    diagnostic_fee_waived = bool(snapshot.get("diagnostic_fee_waived"))

    # Always drop any previously-applied plan savings row so re-applying (e.g. on
    # edit) recomputes cleanly instead of stacking duplicate rows.
    job_document["discounts"] = _strip_plan_discount_rows(job_document.get("discounts"))

    if discount_pct <= 0 and not diagnostic_fee_waived:
        job_document["maintenance_plan_id"] = plan.get("_id")
        job_document["is_maintenance_visit"] = False
        job_document["plan_discount_applied"] = False
        job_document["plan_discount_pct"] = discount_pct
        _resync_total_after_plan_discount(job_document, business_doc, customer_doc)
        return job_document

    # Plan checkbox values are stored lowercase (e.g. "repairs", "parts") while
    # service records use capitalized service types (e.g. "Repairs"), so compare
    # case-insensitively.
    discount_service_types = {
        str(item).strip().lower() for item in (snapshot.get("discount_service_types") or []) if str(item).strip()
    }
    discount_line_item_types = {
        str(item).strip().lower() for item in (snapshot.get("discount_line_item_types") or []) if str(item).strip()
    }

    # A plan may only cover specific HVAC systems on the property. When it does,
    # a line item is only eligible for the discount if it is explicitly tagged to
    # one of the covered systems. Untagged (property-level) items are NOT
    # discounted. A plan with no covered systems applies to the whole property.
    covered_system_ids = {
        str((entry or {}).get("hvac_system_id") or "").strip()
        for entry in ((plan or {}).get("covered_systems") or [])
        if str((entry or {}).get("hvac_system_id") or "").strip()
    }

    def _component_is_covered(component):
        if not covered_system_ids:
            return True
        tagged_system_id = str((component or {}).get("hvac_system_id") or "").strip()
        if not tagged_system_id:
            return False
        return tagged_system_id in covered_system_ids

    def _service_amount(service):
        return _coerce_line_amount(service.get("price") or service.get("standard_price") or 0)

    def _part_amount(part):
        return _coerce_line_amount(part.get("price") or part.get("sell_price") or part.get("unit_cost") or 0)

    def _line_amount(item):
        return _coerce_line_amount(item.get("line_total") or item.get("price") or 0)

    # Compute the savings without mutating line item prices. The original prices
    # stay intact and the savings are represented as a single discount row, so
    # the customer can see the full value the plan provides.
    savings = 0.0

    if discount_pct > 0:
        for service in (job_document.get("services") or []):
            if not isinstance(service, dict) or not _component_is_covered(service):
                continue
            service_type = str(service.get("service_type") or "").strip()
            # Waived diagnostics are fully credited below; don't also percent-off.
            if diagnostic_fee_waived and service_type == "Diagnostics":
                continue
            if service_type.lower() in discount_service_types:
                savings += _service_amount(service) * (discount_pct / 100.0)

        if "parts" in discount_line_item_types:
            for part in (job_document.get("parts") or []):
                if isinstance(part, dict) and _component_is_covered(part):
                    savings += _part_amount(part) * (discount_pct / 100.0)

        if "materials" in discount_line_item_types:
            for material in (job_document.get("materials") or []):
                if isinstance(material, dict) and _component_is_covered(material):
                    savings += _line_amount(material) * (discount_pct / 100.0)

        if "equipment" in discount_line_item_types:
            for equipment in (job_document.get("equipments") or []):
                if isinstance(equipment, dict) and _component_is_covered(equipment):
                    savings += _line_amount(equipment) * (discount_pct / 100.0)

    if diagnostic_fee_waived:
        for service in (job_document.get("services") or []):
            if not isinstance(service, dict) or not _component_is_covered(service):
                continue
            if str(service.get("service_type") or "").strip() == "Diagnostics":
                savings += _service_amount(service)

    savings = round(max(0.0, savings), 2)
    if savings > 0:
        plan_name = str(snapshot.get("name") or "Maintenance Plan").strip() or "Maintenance Plan"
        job_document.setdefault("discounts", [])
        job_document["discounts"].append(
            {
                "discount_name": PLAN_DISCOUNT_ROW_NAME,
                "discount_category": plan_name,
                "discount_percentage": "",
                "discount_amount": normalize_currency(savings),
                "line_total": f"-{normalize_currency(savings)}",
                "source": PLAN_DISCOUNT_SOURCE,
            }
        )

    _resync_total_after_plan_discount(job_document, business_doc, customer_doc)

    job_document["maintenance_plan_id"] = plan.get("_id")
    job_document["is_maintenance_visit"] = False
    job_document["plan_discount_applied"] = savings > 0
    job_document["plan_discount_pct"] = discount_pct
    return job_document


def _resync_total_after_plan_discount(job_document, business_doc, customer_doc):
    pricing_summary = _build_pricing_summary(job_document, business_doc=business_doc, customer_doc=customer_doc)
    new_total = round(_safe_float(pricing_summary.get("total_due"), 0.0), 2)
    job_document["total_amount"] = new_total
    if "balance_due" in job_document:
        amount_paid = _safe_float(job_document.get("total_amount_paid"), 0.0)
        job_document["balance_due"] = round(max(0.0, new_total - amount_paid), 2)
    return new_total


def _calculate_job_payment_state(db, job_doc):
    serialized_job = serialize_doc(job_doc or {})
    job_id = str(serialized_job.get("_id") or "").strip()
    stored_total_amount = round(_safe_float(serialized_job.get("total_amount"), 0.0), 2)

    business_doc = _resolve_business_doc_for_job(db, serialized_job) or {}
    customer_doc = {}
    customer_ref = serialized_job.get("customer_id")
    if customer_ref:
        raw_customer = db.customers.find_one(build_reference_filter("_id", customer_ref))
        if raw_customer:
            customer_doc = serialize_doc(raw_customer)

    pricing_summary = _build_invoice_pricing_summary(
        serialized_job,
        business_doc=serialize_doc(business_doc) if business_doc else {},
        customer_doc=customer_doc,
    )
    total_due = round(_safe_float(pricing_summary.get("total_due"), 0.0), 2)
    if round(_safe_float(pricing_summary.get("subtotal"), 0.0), 2) <= 0 and stored_total_amount > 0:
        total_due = stored_total_amount

    amount_paid = 0.0
    if job_id and ObjectId.is_valid(job_id):
        amount_paid = round(
            sum(
                _safe_float(item.get("amount"), 0.0)
                for item in db.payments.find(_reference_match("job_id", job_id, {"status": "completed"}))
            ),
            2,
        )

    balance_due = round(max(0.0, total_due - amount_paid), 2)
    return {
        "pricing_summary": pricing_summary,
        "total_due": total_due,
        "amount_paid": amount_paid,
        "balance_due": balance_due,
    }


def _format_payment_method_label(method):
    labels = {
        "card": "Card",
        "ach": "ACH",
        "cash": "Cash",
        "check": "Check",
    }
    normalized_method = str(method or "").strip().lower()
    return labels.get(normalized_method, "Unknown")


def _build_payment_history(db, job_id, invoice_ref=""):
    job_filter = _reference_match("job_id", job_id)
    normalized_invoice_ref = str(invoice_ref or "").strip()
    if normalized_invoice_ref:
        invoice_filter = build_reference_filter("invoice_id", normalized_invoice_ref)
        payment_filter = {"$and": [job_filter, invoice_filter]}
    else:
        payment_filter = job_filter

    payment_docs = list(db.payments.find(payment_filter).sort([("paid_at", -1), ("created_at", -1), ("_id", -1)]))
    history_rows = []
    for payment in payment_docs:
        amount = round(_safe_float(payment.get("amount"), 0.0), 2)
        paid_at_value = _coerce_datetime_utc(payment.get("paid_at"))
        paid_at_iso = ""
        if paid_at_value is None:
            date_display = str(payment.get("paid_at") or payment.get("created_at") or "").strip()
        else:
            if paid_at_value.tzinfo is None:
                paid_at_value = paid_at_value.replace(tzinfo=UTC)
            paid_at_iso = paid_at_value.isoformat()
            date_display = paid_at_value.strftime("%m/%d/%Y %H:%M:%S")

        history_rows.append(
            {
                "id": str(payment.get("_id") or "").strip(),
                "date": date_display,
                "date_iso": paid_at_iso,
                "amount": amount,
                "amount_display": normalize_currency(amount),
                "method": _format_payment_method_label(payment.get("payment_method")),
                "status": str(payment.get("status") or "").strip().title() or "Unknown",
                "notes": str(payment.get("notes") or "").strip(),
            }
        )
    return history_rows


def _build_invoice_payment_summary(job_doc, pricing_summary, payment_history):
    summary_payload = pricing_summary or {}
    payload = job_doc or {}
    total_due = round(_safe_float(summary_payload.get("total_due"), 0.0), 2)
    amount_paid = round(_safe_float(payload.get("total_amount_paid"), 0.0), 2)
    if amount_paid <= 0 and payment_history:
        amount_paid = round(sum(_safe_float(item.get("amount"), 0.0) for item in payment_history if str(item.get("status") or "").strip().lower() == "completed"), 2)

    invoice_balance = round(max(0.0, total_due - amount_paid), 2)
    payment_state = str(payload.get("payment_status") or "").strip().lower()
    status_label_map = {
        "pending_paid": "Payment pending",
        "partial_paid": "Partially paid",
        "paid": "Paid in full",
    }
    latest_payment = payment_history[0] if payment_history else {}

    return {
        "show": bool(payment_history) or amount_paid > 0,
        "status_label": status_label_map.get(payment_state, "Payment pending" if invoice_balance > 0 else "Paid in full"),
        "amount_paid": amount_paid,
        "amount_paid_display": normalize_currency(amount_paid),
        "invoice_balance": invoice_balance,
        "invoice_balance_display": normalize_currency(invoice_balance),
        "payment_date": str(latest_payment.get("date") or "").strip(),
        "payment_date_iso": str(latest_payment.get("date_iso") or "").strip(),
        "payment_channel_label": str(latest_payment.get("method") or "Pending"),
    }


def _resolve_invoice_status(invoice_entry, job_doc=None):
    invoice_payload = invoice_entry or {}
    normalized_status = str(invoice_payload.get("status") or "").strip().title()
    if normalized_status in {"Created", "Sent", "Paid"}:
        return normalized_status

    job_status = str((job_doc or {}).get("status") or "").strip().lower()
    payment_status = str((job_doc or {}).get("payment_status") or "").strip().lower()
    if payment_status == "paid" or job_status == "paid":
        return "Paid"

    if str(invoice_payload.get("date_sent") or "").strip() or str(invoice_payload.get("date_sent_utc") or "").strip() or str((job_doc or {}).get("date_invoice_sent") or "").strip():
        return "Sent"

    return "Created"


def _resolve_invoice_sent_display(invoice_entry, job_doc=None):
    invoice_payload = invoice_entry or {}
    raw_value = str(
        invoice_payload.get("sent_at")
        or invoice_payload.get("date_sent")
        or invoice_payload.get("date_sent_utc")
        or (job_doc or {}).get("date_invoice_sent")
        or ""
    ).strip()
    if not raw_value:
        return ""

    if "/" in raw_value and ":" in raw_value:
        return raw_value

    normalized_value = raw_value.replace("Z", "+00:00")
    try:
        parsed_value = datetime.fromisoformat(normalized_value)
    except ValueError:
        return raw_value

    return parsed_value.strftime("%m/%d/%Y %H:%M:%S")


def _resolve_invoice_sent_iso(invoice_entry, job_doc=None):
    invoice_payload = invoice_entry or {}
    raw_value = str(
        invoice_payload.get("sent_at")
        or invoice_payload.get("date_sent")
        or invoice_payload.get("date_sent_utc")
        or (job_doc or {}).get("date_invoice_sent")
        or ""
    ).strip()
    if not raw_value:
        return ""

    normalized_value = raw_value.replace("Z", "+00:00")
    try:
        parsed_value = datetime.fromisoformat(normalized_value)
    except ValueError:
        return ""

    if parsed_value.tzinfo is None:
        parsed_value = parsed_value.replace(tzinfo=UTC)
    return parsed_value.isoformat()


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
        system_nickname = str(doc.get("system_nickname") or "").strip()
        title = f"{system_type} - {system_nickname}" if system_nickname else system_type
        result.append({
            "id": str(doc.get("_id")),
            "title": title,
            "system_type": system_type,
            "system_nickname": system_nickname,
        })
    return result


def _build_hvac_system_lookup_for_property(db, customer_id, property_id):
    systems = _query_hvac_systems_for_property(db, customer_id, property_id)
    return {
        str(system.get("id") or "").strip(): str(system.get("title") or "").strip()
        for system in systems
        if str(system.get("id") or "").strip()
    }


def _apply_hvac_tags_to_components(components, form_field_values, hvac_lookup):
    """Patch single HVAC tag metadata onto each component dict."""
    normalized_lookup = hvac_lookup or {}
    for i, component in enumerate(components):
        raw_hvac_system_id = str(form_field_values[i] if i < len(form_field_values) else "").strip()
        if raw_hvac_system_id and raw_hvac_system_id in normalized_lookup:
            component["hvac_system_id"] = raw_hvac_system_id
            component["hvac_system_name"] = normalized_lookup.get(raw_hvac_system_id)
            component["tag_level"] = "system"
        else:
            component["hvac_system_id"] = None
            component["hvac_system_name"] = None
            component["tag_level"] = "property"
        if isinstance(component, dict):
            component.pop("hvac_system_ids", None)
    return components


def _collect_tagged_hvac_system_ids(job_doc):
    component_types = ("services", "parts", "labors", "materials", "equipments")
    hvac_system_ids = set()

    for component_type in component_types:
        for component in (job_doc.get(component_type) or []):
            if not isinstance(component, dict):
                continue

            single_tag_id = str(component.get("hvac_system_id") or "").strip()
            if single_tag_id:
                hvac_system_ids.add(single_tag_id)

            for hvac_id in (component.get("hvac_system_ids") or []):
                normalized_hvac_id = str(hvac_id or "").strip()
                if normalized_hvac_id:
                    hvac_system_ids.add(normalized_hvac_id)

    return hvac_system_ids


def _mark_hvac_systems_serviced(db, job_doc, completed_at):
    customer_id = (job_doc or {}).get("customer_id")
    if not customer_id:
        return 0

    hvac_system_ids = _collect_tagged_hvac_system_ids(job_doc or {})
    if not hvac_system_ids:
        return 0

    assigned_employee = str((job_doc or {}).get("assigned_employee") or "").strip()
    updated_count = 0

    for hvac_system_id in hvac_system_ids:
        hvac_query = {
            "$and": [
                build_reference_filter("_id", hvac_system_id),
                build_reference_filter("customer_id", customer_id),
            ]
        }
        result = db.hvacSystems.update_one(
            hvac_query,
            {
                "$set": {
                    "last_serviced_at": completed_at,
                    "last_serviced_by": assigned_employee,
                    "updated_at": completed_at,
                }
            },
        )
        updated_count += int(result.modified_count or 0)

    return updated_count


@bp.route("/api/hvac-systems-for-property")
def api_hvac_systems_for_property():
    if not _is_authenticated_employee():
        return jsonify({"error": "Unauthorized"}), 401
    db = ensure_connection_or_500()
    customer_id = request.args.get("customer_id", "").strip()
    property_id = request.args.get("property_id", "").strip()
    systems = _query_hvac_systems_for_property(db, customer_id, property_id)
    return jsonify({"hvac_systems": systems})


@bp.route("/customers/<customerId>/jobs/plan-discount-preview", methods=["POST"])
def plan_discount_preview(customerId):
    """Live preview of the maintenance-plan discount that will apply to a draft job.

    Accepts the same form fields submitted by the create/update job forms and
    reuses the server-side discount logic so the preview always matches what the
    save will actually do.
    """
    if not _is_authenticated_employee():
        return jsonify({"error": "Unauthorized"}), 401

    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return jsonify({"has_plan": False}), 404

    business_id = resolve_current_business_id(db)
    selected_property_id = request.form.get("job_property_id", "").strip()

    active_plan = get_active_plan_for_property(db, selected_property_id, business_id)
    if not active_plan:
        return jsonify({"has_plan": False})

    snapshot = active_plan.get("template_snapshot") or {}
    try:
        discount_pct = float(snapshot.get("repair_discount_pct") or 0)
    except (TypeError, ValueError):
        discount_pct = 0.0
    diagnostic_fee_waived = bool(snapshot.get("diagnostic_fee_waived"))
    discount_service_types = [
        str(item).strip().title() for item in (snapshot.get("discount_service_types") or []) if str(item).strip()
    ]
    discount_line_item_types = [
        str(item).strip().title() for item in (snapshot.get("discount_line_item_types") or []) if str(item).strip()
    ]

    plan_info = {
        "has_plan": True,
        "plan_name": str(snapshot.get("name") or "Maintenance Plan").strip() or "Maintenance Plan",
        "plan_number": str(active_plan.get("plan_number") or "").strip(),
    }

    if discount_pct <= 0 and not diagnostic_fee_waived:
        return jsonify({**plan_info, "has_discount": False})

    service_query = {"business_id": business_id} if business_id else {"_id": None}
    part_query = {"business_id": business_id} if business_id else {"_id": None}
    material_query = {"business_id": business_id} if business_id else {"_id": None}
    equipment_query = {"business_id": business_id} if business_id else {"_id": None}
    discount_query = {"business_id": business_id} if business_id else {"_id": None}

    service_docs = [serialize_doc(doc) for doc in db.services.find(service_query).sort("service_name", 1)]
    part_docs = [_serialize_part_without_legacy_fields(doc) for doc in db.parts.find(part_query).sort("part_name", 1)]
    material_docs = [serialize_doc(doc) for doc in db.materials.find(material_query).sort("material_name", 1)]
    equipment_docs = [serialize_doc(doc) for doc in db.equipment.find(equipment_query).sort("equipment_name", 1)]
    discount_docs = [serialize_doc(doc) for doc in db.discounts.find(discount_query).sort("discount_name", 1)]

    service_catalog = build_service_catalog(service_docs)
    part_catalog = build_part_catalog(part_docs)
    material_catalog = build_material_catalog(material_docs)
    equipment_catalog = build_equipment_catalog(equipment_docs)
    discount_catalog = build_discount_catalog(discount_docs)
    business_doc_for_rates = serialize_doc(db.businesses.find_one({"_id": business_id})) if business_id else {}
    customer_doc = serialize_doc(customer)

    services, _ = build_job_services_from_form(
        request.form.getlist("service_code[]") or request.form.getlist("service_type[]"),
        request.form.getlist("service_price[]") or request.form.getlist("service_standard_price[]"),
        request.form.getlist("service_hours[]") or request.form.getlist("service_estimated_hours[]") or request.form.getlist("service_duration[]"),
        service_catalog,
        business_doc_for_rates.get("labor_rate_standard"),
        request.form.getlist("service_emergency_call[]"),
    )
    parts, _ = build_job_parts_from_form(
        request.form.getlist("part_code[]") or request.form.getlist("part_name[]"),
        request.form.getlist("part_unit_cost[]") or request.form.getlist("part_price[]"),
        part_catalog,
    )
    materials, _ = build_job_materials_from_form(
        request.form.getlist("material_name[]"),
        request.form.getlist("material_quantity_used[]"),
        request.form.getlist("material_unit_of_measure[]"),
        request.form.getlist("material_price[]"),
        material_catalog,
    )
    equipments, _ = build_job_equipments_from_form(
        request.form.getlist("equipment_name[]"),
        request.form.getlist("equipment_quantity_installed[]"),
        request.form.getlist("equipment_price[]"),
        request.form.getlist("equipment_serial_number[]"),
        equipment_catalog,
    )
    discounts, _ = build_job_discounts_from_form(
        request.form.getlist("discount_name[]"),
        request.form.getlist("discount_percentage[]"),
        request.form.getlist("discount_amount[]"),
        discount_catalog,
    )

    # Tag line items to their selected HVAC systems so the discount preview can
    # respect plans that only cover specific systems.
    hvac_lookup = _build_hvac_system_lookup_for_property(db, customerId, selected_property_id)
    _apply_hvac_tags_to_components(services, request.form.getlist("service_hvac_system_id[]"), hvac_lookup)
    _apply_hvac_tags_to_components(parts, request.form.getlist("part_hvac_system_id[]"), hvac_lookup)
    _apply_hvac_tags_to_components(materials, request.form.getlist("material_hvac_system_id[]"), hvac_lookup)
    _apply_hvac_tags_to_components(equipments, request.form.getlist("equipment_hvac_system_id[]"), hvac_lookup)

    draft = {
        "services": services,
        "parts": parts,
        "labors": [],
        "materials": materials,
        "equipments": equipments,
        "discounts": _strip_plan_discount_rows(discounts),
    }

    baseline_summary = _build_pricing_summary(draft, business_doc=business_doc_for_rates, customer_doc=customer_doc)
    original_total = round(_safe_float(baseline_summary.get("total_due"), 0.0), 2)

    response = {
        **plan_info,
        "has_discount": True,
        "discount_pct": discount_pct,
        "diagnostic_fee_waived": diagnostic_fee_waived,
        "service_types": discount_service_types,
        "line_item_types": discount_line_item_types,
    }

    discounted_draft = copy.deepcopy(draft)
    apply_plan_discount(
        discounted_draft,
        active_plan,
        business_doc=business_doc_for_rates,
        customer_doc=customer_doc,
    )
    discounted_total = round(_safe_float(discounted_draft.get("total_amount"), original_total), 2)
    savings = round(max(0.0, original_total - discounted_total), 2)

    return jsonify({
        **response,
        "already_applied": False,
        "original_total": original_total,
        "discounted_total": discounted_total,
        "savings": savings,
    })


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
        selected_material_names = request.form.getlist("material_name[]")
        entered_material_quantities = request.form.getlist("material_quantity_used[]")
        entered_material_units = request.form.getlist("material_unit_of_measure[]")
        entered_material_prices = request.form.getlist("material_price[]")
        selected_equipment_names = request.form.getlist("equipment_name[]")
        entered_equipment_quantities = request.form.getlist("equipment_quantity_installed[]")
        entered_equipment_prices = request.form.getlist("equipment_price[]")
        entered_equipment_serial_numbers = request.form.getlist("equipment_serial_number[]")
        selected_discount_names = request.form.getlist("discount_name[]")
        entered_discount_percentages = request.form.getlist("discount_percentage[]")
        entered_discount_amounts = request.form.getlist("discount_amount[]")
        service_query = {"business_id": business_id} if business_id else {"_id": None}
        part_query = {"business_id": business_id} if business_id else {"_id": None}
        material_query = {"business_id": business_id} if business_id else {"_id": None}
        equipment_query = {"business_id": business_id} if business_id else {"_id": None}
        discount_query = {"business_id": business_id} if business_id else {"_id": None}
        service_docs = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
        part_docs = [_serialize_part_without_legacy_fields(part) for part in db.parts.find(part_query).sort("part_name", 1)]
        material_docs = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
        equipment_docs = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
        discount_docs = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
        service_catalog = build_service_catalog(service_docs)
        part_catalog = build_part_catalog(part_docs)
        material_catalog = build_material_catalog(material_docs)
        equipment_catalog = build_equipment_catalog(equipment_docs)
        discount_catalog = build_discount_catalog(discount_docs)
        business_doc_for_rates = serialize_doc(db.businesses.find_one({"_id": business_id})) if business_id else {}
        services, services_total = build_job_services_from_form(
            selected_service_types,
            entered_service_prices,
            entered_service_durations,
            service_catalog,
            business_doc_for_rates.get("labor_rate_standard"),
            entered_service_emergency_calls,
        )
        parts, parts_total = build_job_parts_from_form(
            selected_part_names,
            entered_part_prices,
            part_catalog,
        )
        labors = []
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
            entered_equipment_serial_numbers,
            equipment_catalog,
        )
        discounts, discounts_total = build_job_discounts_from_form(
            selected_discount_names,
            entered_discount_percentages,
            entered_discount_amounts,
            discount_catalog,
        )
        hvac_lookup = _build_hvac_system_lookup_for_property(db, customerId, selected_property_id)
        _apply_hvac_tags_to_components(services, request.form.getlist("service_hvac_system_id[]"), hvac_lookup)
        _apply_hvac_tags_to_components(parts, request.form.getlist("part_hvac_system_id[]"), hvac_lookup)
        _apply_hvac_tags_to_components(materials, request.form.getlist("material_hvac_system_id[]"), hvac_lookup)
        _apply_hvac_tags_to_components(equipments, request.form.getlist("equipment_hvac_system_id[]"), hvac_lookup)
        pricing_summary = _build_pricing_summary(
            {
                "services": services,
                "parts": parts,
                "labors": labors,
                "materials": materials,
                "equipments": equipments,
                "discounts": discounts,
            },
            business_doc=business_doc_for_rates,
            customer_doc=serialize_doc(customer),
        )
        total = pricing_summary["total_due"]
        payment_schedule = _build_payment_schedule_for_record(
            {"services": services, "payment_schedule": []},
            business_doc=business_doc_for_rates,
            total_amount=total,
            raw_schedule=request.form.get("payment_schedule_json", ""),
        )
        payment_schedule = _build_payment_schedule_for_record(
            {"services": services, "payment_schedule": []},
            business_doc=business_doc_for_rates,
            total_amount=total,
            raw_schedule=request.form.get("payment_schedule_json", ""),
        )

        primary_service = services[0]["type"] if services else "No services added."
        scheduled_date = format_date(request.form.get("job_date", ""))
        scheduled_time = request.form.get("job_time", "").strip()
        payment_due_days = _normalize_payment_due_days(
            request.form.get("payment_due_days", ""),
            default_payment_due_days,
        )
        payment_due_days_offset = payment_due_days
        date_scheduled = datetime.now().strftime("%m/%d/%Y") if (scheduled_date and scheduled_time) else ""
        primary_technician_id = _resolve_employee_id_value(db, request.form.get("primary_technician_id", "") or request.form.get("job_assigned_employee", ""))
        technician_payload = _build_job_technician_payload(
            db,
            primary_technician_id,
            request.form.getlist("additional_technician_ids[]"),
        )
        job_status = resolve_job_status(
            scheduled_date,
            scheduled_time,
            services,
            parts,
            labors,
            materials,
            equipments,
            discounts,
            primary_technician_id=technician_payload.get("primary_technician_id") or "",
        )

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
                technician_payload,
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
            "primary_technician_id": technician_payload.get("primary_technician_id") or None,
            "additional_technician_ids": technician_payload.get("additional_technician_ids") or [],
            "additional_technician_names": technician_payload.get("additional_technician_names") or [],
            "assigned_employee": technician_payload.get("assigned_employee") or "",
            "total_amount": float(total or 0.0),
            "invoice_notes": invoice_notes,
            "payment_due_days": payment_due_days,
            "payment_schedule": payment_schedule,
            "internal_notes": [],
            "date_created": datetime.now().strftime("%m/%d/%Y"),
            "created_at": datetime.now(UTC),
            "invoices": [],
            "total_amount_paid": 0.0,
            "balance_due": float(total or 0.0),
            "payment_status": "pending_paid",
            "paid_at": None,
            "business_id": business_id,
            "job_kind": "one_time",
            "series_id": None,
            "occurrence_index": None,
            "recurrence_summary": "",
        }
        active_plan = get_active_plan_for_property(db, new_job.get("property_id"), business_id)
        if active_plan:
            new_job = apply_plan_discount(
                new_job,
                active_plan,
                business_doc=business_doc_for_rates,
                customer_doc=serialize_doc(customer),
            )
        inserted = db.jobs.insert_one(new_job)
        if payment_schedule:
            _ensure_job_invoice_entry(db, str(inserted.inserted_id))
        current_app.logger.info("Job created: id=%s customer_id=%s by employee_id=%s", str(inserted.inserted_id), customerId, session.get("employee_id"))
        return redirect(url_for("jobs.view_job", jobId=str(inserted.inserted_id)))

    business_id = resolve_current_business_id(db)
    service_query = {"business_id": business_id} if business_id else {"_id": None}
    part_query = {"business_id": business_id} if business_id else {"_id": None}
    material_query = {"business_id": business_id} if business_id else {"_id": None}
    equipment_query = {"business_id": business_id} if business_id else {"_id": None}
    discount_query = {"business_id": business_id} if business_id else {"_id": None}
    category_query = {"company_id": str(business_id)} if business_id else {"_id": None}
    services = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
    parts = [_serialize_part_without_legacy_fields(part) for part in db.parts.find(part_query).sort("part_name", 1)]
    materials = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
    equipments = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
    discounts = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
    part_categories = [serialize_doc(category) for category in db.categories.find({**category_query, "type": "part"}).sort("name", 1)]
    material_categories = [serialize_doc(category) for category in db.categories.find({**category_query, "type": "material"}).sort("name", 1)]
    equipment_categories = [serialize_doc(category) for category in db.categories.find({**category_query, "type": "equipment"}).sort("name", 1)]
    employee_options = build_employee_options(db)
    services_catalog = build_service_catalog(services)
    parts_catalog = build_part_catalog(parts)
    materials_catalog = build_material_catalog(materials)
    equipments_catalog = build_equipment_catalog(equipments)
    discounts_catalog = build_discount_catalog(discounts)
    parts_by_id = {p["_id"]: p.get("part_name", "") for p in parts}
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
        materials=materials,
        equipments=equipments,
        discounts=discounts,
        part_categories=part_categories,
        material_categories=material_categories,
        equipment_categories=equipment_categories,
        employee_options=employee_options,
        services_catalog=services_catalog,
        parts_catalog=parts_catalog,
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
        selected_material_names = request.form.getlist("material_name[]")
        entered_material_quantities = request.form.getlist("material_quantity_used[]")
        entered_material_units = request.form.getlist("material_unit_of_measure[]")
        entered_material_prices = request.form.getlist("material_price[]")
        selected_equipment_names = request.form.getlist("equipment_name[]")
        entered_equipment_quantities = request.form.getlist("equipment_quantity_installed[]")
        entered_equipment_prices = request.form.getlist("equipment_price[]")
        entered_equipment_serial_numbers = request.form.getlist("equipment_serial_number[]")
        selected_discount_names = request.form.getlist("discount_name[]")
        entered_discount_percentages = request.form.getlist("discount_percentage[]")
        entered_discount_amounts = request.form.getlist("discount_amount[]")

        service_query = {"business_id": business_id} if business_id else {"_id": None}
        part_query = {"business_id": business_id} if business_id else {"_id": None}
        material_query = {"business_id": business_id} if business_id else {"_id": None}
        equipment_query = {"business_id": business_id} if business_id else {"_id": None}
        discount_query = {"business_id": business_id} if business_id else {"_id": None}
        service_docs = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
        part_docs = [_serialize_part_without_legacy_fields(part) for part in db.parts.find(part_query).sort("part_name", 1)]
        material_docs = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
        equipment_docs = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
        discount_docs = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
        service_catalog = build_service_catalog(service_docs)
        part_catalog = build_part_catalog(part_docs)
        material_catalog = build_material_catalog(material_docs)
        equipment_catalog = build_equipment_catalog(equipment_docs)
        discount_catalog = build_discount_catalog(discount_docs)

        business_doc_for_rates = serialize_doc(db.businesses.find_one({"_id": business_id})) if business_id else {}
        services, services_total = build_job_services_from_form(
            selected_service_types,
            entered_service_prices,
            entered_service_durations,
            service_catalog,
            business_doc_for_rates.get("labor_rate_standard"),
            entered_service_emergency_calls,
        )
        parts, parts_total = build_job_parts_from_form(
            selected_part_names,
            entered_part_prices,
            part_catalog,
        )
        labors = []
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
            entered_equipment_serial_numbers,
            equipment_catalog,
        )
        discounts, discounts_total = build_job_discounts_from_form(
            selected_discount_names,
            entered_discount_percentages,
            entered_discount_amounts,
            discount_catalog,
        )
        hvac_lookup = _build_hvac_system_lookup_for_property(db, customerId, selected_property_id)
        _apply_hvac_tags_to_components(services, request.form.getlist("service_hvac_system_id[]"), hvac_lookup)
        _apply_hvac_tags_to_components(parts, request.form.getlist("part_hvac_system_id[]"), hvac_lookup)
        _apply_hvac_tags_to_components(materials, request.form.getlist("material_hvac_system_id[]"), hvac_lookup)
        _apply_hvac_tags_to_components(equipments, request.form.getlist("equipment_hvac_system_id[]"), hvac_lookup)
        pricing_summary = _build_pricing_summary(
            {
                "services": services,
                "parts": parts,
                "labors": labors,
                "materials": materials,
                "equipments": equipments,
                "discounts": discounts,
            },
            business_doc=business_doc_for_rates,
            customer_doc=serialize_doc(customer),
        )
        total = pricing_summary["total_due"]
        payment_schedule = _build_payment_schedule_for_record(
            {"services": services, "payment_schedule": []},
            business_doc=business_doc_for_rates,
            total_amount=total,
            raw_schedule=request.form.get("payment_schedule_json", ""),
        )

        primary_technician_id = _resolve_employee_id_value(db, request.form.get("primary_technician_id", "") or request.form.get("job_assigned_employee", ""))
        technician_payload = _build_job_technician_payload(db, primary_technician_id, [])
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
            "date_customer_viewed": "",
            "address_line_1": request.form.get("job_address_line_1", "").strip(),
            "address_line_2": request.form.get("job_address_line_2", "").strip(),
            "city": request.form.get("job_city", "").strip(),
            "state": request.form.get("job_state", "").strip().upper(),
            "zip_code": request.form.get("job_zip_code", "").strip(),
            "created_by_employee": (session.get("employee_name") or "").strip(),
            "primary_technician_id": technician_payload.get("primary_technician_id") or None,
            "estimated_by_employee": technician_payload.get("assigned_employee") or "",
            "assigned_employee": technician_payload.get("assigned_employee") or "",
            "proposed_job_date": proposed_job_date,
            "proposed_job_time": proposed_job_time,
            "job_schedule_type": recurrence_data.get("schedule_type") or "one_time",
            "recurring_frequency": recurrence_data.get("frequency") or "",
            "recurring_end_type": recurrence_data.get("end_type") or "never",
            "recurring_end_date": recurrence_data.get("end_date") or "",
            "recurring_end_after": recurrence_data.get("max_occurrences"),
            "recurrence_summary": recurrence_data.get("summary") or "",
            "total_amount": float(total or 0.0),
            "payment_schedule": payment_schedule,
            "estimate_notes": estimate_notes,
            "estimate_expiration_days": estimate_expiration_days,
            "file_path": [],
            "latest_file_path": "",
            "business_id": business_id,
            "created_at": datetime.now(UTC),
        }

        active_plan = get_active_plan_for_property(db, new_estimate.get("property_id"), business_id)
        if active_plan:
            new_estimate = apply_plan_discount(
                new_estimate,
                active_plan,
                business_doc=business_doc_for_rates,
                customer_doc=serialize_doc(customer),
            )

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
                        "tax_rates": 1,
                    },
                )
                if business_doc:
                    business_payload = serialize_doc(business_doc)
            estimate_pdf_payload = _hydrate_service_descriptions_for_pdf(
                db,
                serialize_estimate_for_pdf(new_estimate),
                business_id=business_id,
            )
            estimate_pdf_path = generate_estimate(
                estimate_id,
                estimate_pdf_payload,
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
    material_query = {"business_id": business_id} if business_id else {"_id": None}
    equipment_query = {"business_id": business_id} if business_id else {"_id": None}
    discount_query = {"business_id": business_id} if business_id else {"_id": None}
    category_query = {"company_id": str(business_id)} if business_id else {"_id": None}
    services = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
    parts = [_serialize_part_without_legacy_fields(part) for part in db.parts.find(part_query).sort("part_name", 1)]
    materials = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
    equipments = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
    discounts = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
    part_categories = [serialize_doc(category) for category in db.categories.find({**category_query, "type": "part"}).sort("name", 1)]
    material_categories = [serialize_doc(category) for category in db.categories.find({**category_query, "type": "material"}).sort("name", 1)]
    equipment_categories = [serialize_doc(category) for category in db.categories.find({**category_query, "type": "equipment"}).sort("name", 1)]
    employee_options = build_employee_options(db)
    services_catalog = build_service_catalog(services)
    parts_catalog = build_part_catalog(parts)
    materials_catalog = build_material_catalog(materials)
    equipments_catalog = build_equipment_catalog(equipments)
    discounts_catalog = build_discount_catalog(discounts)
    parts_by_id = {p["_id"]: p.get("part_name", "") for p in parts}
    materials_by_id = {m["_id"]: m["material_name"] for m in materials}
    default_property = _resolve_default_property(customer)
    selected_property_id = str((default_property or {}).get("property_id") or "").strip()
    initial_hvac_systems = _query_hvac_systems_for_property(db, customerId, selected_property_id)
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
        initial_hvac_systems=initial_hvac_systems,
        services=services,
        parts=parts,
        materials=materials,
        equipments=equipments,
        discounts=discounts,
        part_categories=part_categories,
        material_categories=material_categories,
        equipment_categories=equipment_categories,
        employee_options=employee_options,
        services_catalog=services_catalog,
        parts_catalog=parts_catalog,
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
    if is_staff_view and not has_customer_token and not _doc_belongs_to_current_business(db, estimate):
        return redirect(url_for("home"))
    if not is_staff_view and not has_customer_token:
        return redirect(url_for("auth.login"))

    if not is_staff_view and has_customer_token:
        existing_customer_view_date = str(estimate.get("date_customer_viewed") or "").strip()
        if not existing_customer_view_date:
            customer_viewed_at = datetime.now().strftime("%m/%d/%Y %I:%M %p")
            db.estimates.update_one(
                {"_id": ObjectId(estimateId)},
                {"$set": {"date_customer_viewed": customer_viewed_at}},
            )
            estimate["date_customer_viewed"] = customer_viewed_at

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
    estimate_business = {}
    estimate_business_id = str(estimate_doc.get("business_id") or "").strip()
    if estimate_business_id:
        estimate_business_doc = db.businesses.find_one(build_reference_filter("_id", estimate_business_id))
        if estimate_business_doc:
            estimate_business = serialize_doc(estimate_business_doc)
    pricing_summary = _build_estimate_pricing_summary(estimate_doc, business_doc=estimate_business, customer_doc=customer)
    payment_schedule_view = _build_payment_schedule_view(estimate_doc.get("payment_schedule") or [])
    payment_request_state = str(request.args.get("payment_request") or "").strip().lower()
    payment_request_reason = str(request.args.get("payment_request_reason") or "").strip()
    deposit_prompt = {
        "show": False,
        "label": "",
        "amount": 0.0,
        "amount_display": "",
        "payment_url": "",
        "stage_id": "",
        "request_sent": False,
    }

    created_job_id = str(estimate_doc.get("created_job_id") or "").strip()
    if not is_staff_view and str(estimate_doc.get("status") or "").strip().lower() == "accepted" and ObjectId.is_valid(created_job_id):
        created_job = db.jobs.find_one({"_id": ObjectId(created_job_id)})
        if created_job:
            created_job_doc = serialize_doc(created_job)
            invoice_entry = _ensure_job_invoice_entry(db, created_job_id)
            due_now_amount = _payment_schedule_due_now_amount(created_job_doc.get("payment_schedule") or [])
            due_stage_name = "Payment"
            due_stage_id = ""
            request_sent = False
            for stage in created_job_doc.get("payment_schedule") or []:
                if not isinstance(stage, dict):
                    continue
                if str(stage.get("status") or "").strip().lower() in {"due", "partial"}:
                    due_stage_name = str(stage.get("name") or "Payment").strip() or "Payment"
                    due_stage_id = str(stage.get("stage_id") or "").strip()
                    request_sent = bool(stage.get("request_sent_at"))
                    break
            if invoice_entry and due_now_amount > 0:
                invoice_ref = _resolve_invoice_ref(invoice_entry)
                invoice_token = _issue_invoice_access_token(db, created_job_id, invoice_ref, str(customer.get("email") or "").strip())
                deposit_prompt = {
                    "show": True,
                    "label": due_stage_name,
                    "amount": due_now_amount,
                    "amount_display": normalize_currency(due_now_amount),
                    "payment_url": _build_invoice_view_url(created_job_id, invoice_ref, access_token=invoice_token, external=False),
                    "stage_id": due_stage_id,
                    "request_sent": request_sent,
                }

    return render_template(
        "estimates/view_estimate.html",
        estimateId=estimateId,
        estimate=estimate_doc,
        customer=customer,
        estimate_business=estimate_business,
        quote_email_template=quote_email_template,
        is_staff_view=is_staff_view,
        access_token=token_value,
        pricing_summary=pricing_summary,
        payment_schedule_view=payment_schedule_view,
        payment_request_state=payment_request_state,
        payment_request_reason=payment_request_reason,
        deposit_prompt=deposit_prompt,
    )


@bp.route("/estimates/<estimateId>/update", methods=["GET", "POST"])
def update_estimate(estimateId):
    db = ensure_connection_or_500()
    estimate = db.estimates.find_one({"_id": object_id_or_404(estimateId)})
    if not estimate:
        return redirect(url_for("home"))

    if not _doc_belongs_to_current_business(db, estimate):
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
        selected_material_names = request.form.getlist("material_name[]")
        entered_material_quantities = request.form.getlist("material_quantity_used[]")
        entered_material_units = request.form.getlist("material_unit_of_measure[]")
        entered_material_prices = request.form.getlist("material_price[]")
        selected_equipment_names = request.form.getlist("equipment_name[]")
        entered_equipment_quantities = request.form.getlist("equipment_quantity_installed[]")
        entered_equipment_prices = request.form.getlist("equipment_price[]")
        entered_equipment_serial_numbers = request.form.getlist("equipment_serial_number[]")
        selected_discount_names = request.form.getlist("discount_name[]")
        entered_discount_percentages = request.form.getlist("discount_percentage[]")
        entered_discount_amounts = request.form.getlist("discount_amount[]")

        service_query = {"business_id": business_id} if business_id else {"_id": None}
        part_query = {"business_id": business_id} if business_id else {"_id": None}
        material_query = {"business_id": business_id} if business_id else {"_id": None}
        equipment_query = {"business_id": business_id} if business_id else {"_id": None}
        discount_query = {"business_id": business_id} if business_id else {"_id": None}
        service_docs = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
        part_docs = [_serialize_part_without_legacy_fields(part) for part in db.parts.find(part_query).sort("part_name", 1)]
        material_docs = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
        equipment_docs = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
        discount_docs = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
        service_catalog = build_service_catalog(service_docs)
        part_catalog = build_part_catalog(part_docs)
        material_catalog = build_material_catalog(material_docs)
        equipment_catalog = build_equipment_catalog(equipment_docs)
        discount_catalog = build_discount_catalog(discount_docs)

        business_doc_for_rates = serialize_doc(db.businesses.find_one({"_id": business_id})) if business_id else {}
        services, services_total = build_job_services_from_form(
            selected_service_types,
            entered_service_prices,
            entered_service_durations,
            service_catalog,
            business_doc_for_rates.get("labor_rate_standard"),
            entered_service_emergency_calls,
        )
        parts, parts_total = build_job_parts_from_form(
            selected_part_names,
            entered_part_prices,
            part_catalog,
        )
        labors = []
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
            entered_equipment_serial_numbers,
            equipment_catalog,
        )
        discounts, discounts_total = build_job_discounts_from_form(
            selected_discount_names,
            entered_discount_percentages,
            entered_discount_amounts,
            discount_catalog,
        )
        hvac_lookup = _build_hvac_system_lookup_for_property(db, estimate.get("customer_id"), selected_property_id)
        _apply_hvac_tags_to_components(services, request.form.getlist("service_hvac_system_id[]"), hvac_lookup)
        _apply_hvac_tags_to_components(parts, request.form.getlist("part_hvac_system_id[]"), hvac_lookup)
        _apply_hvac_tags_to_components(materials, request.form.getlist("material_hvac_system_id[]"), hvac_lookup)
        _apply_hvac_tags_to_components(equipments, request.form.getlist("equipment_hvac_system_id[]"), hvac_lookup)
        customer = {}
        customer_id = estimate.get("customer_id")
        if customer_id:
            customer_doc = db.customers.find_one(build_reference_filter("_id", customer_id))
            if customer_doc:
                customer = serialize_doc(customer_doc)

        plan_discount_fields = {}
        selected_property_id_for_plan = selected_property_id if selected_property else ""
        active_plan = get_active_plan_for_property(db, selected_property_id_for_plan, business_id)
        if active_plan:
            plan_draft = {
                "services": services,
                "parts": parts,
                "labors": labors,
                "materials": materials,
                "equipments": equipments,
                "discounts": discounts,
            }
            plan_draft = apply_plan_discount(
                plan_draft,
                active_plan,
                business_doc=business_doc_for_rates,
                customer_doc=customer,
            )
            discounts = plan_draft.get("discounts", discounts)
            for field in ("maintenance_plan_id", "is_maintenance_visit", "plan_discount_applied", "plan_discount_pct"):
                if field in plan_draft:
                    plan_discount_fields[field] = plan_draft[field]

        pricing_summary = _build_pricing_summary(
            {
                "services": services,
                "parts": parts,
                "labors": labors,
                "materials": materials,
                "equipments": equipments,
                "discounts": discounts,
            },
            business_doc=business_doc_for_rates,
            customer_doc=customer,
        )
        total = pricing_summary["total_due"]
        payment_schedule = _build_payment_schedule_for_record(
            {"services": services, "payment_schedule": estimate.get("payment_schedule") or []},
            business_doc=business_doc_for_rates,
            total_amount=total,
            raw_schedule=request.form.get("payment_schedule_json", ""),
        )

        primary_technician_id = _resolve_employee_id_value(db, request.form.get("primary_technician_id", "") or request.form.get("job_assigned_employee", ""))
        technician_payload = _build_job_technician_payload(db, primary_technician_id, [])
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
            "primary_technician_id": technician_payload.get("primary_technician_id") or None,
            "estimated_by_employee": technician_payload.get("assigned_employee") or "",
            "assigned_employee": technician_payload.get("assigned_employee") or "",
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
            "payment_schedule": payment_schedule,
            "date_updated": datetime.now().strftime("%m/%d/%Y"),
            "time_updated": datetime.now().strftime("%H:%M:%S"),
            "updated_at": datetime.now(UTC),
        }
        if business_id and not estimate.get("business_id"):
            updated_data["business_id"] = business_id

        if plan_discount_fields:
            updated_data.update(plan_discount_fields)

        estimate_for_pdf = dict(estimate)
        estimate_for_pdf.update(updated_data)
        customer = customer or {}

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
                    "tax_rates": 1,
                },
            )
            if business_doc:
                business_payload = serialize_doc(business_doc)
        previous_file_path = resolve_estimate_file_path(estimate)
        estimate_pdf_payload = _hydrate_service_descriptions_for_pdf(
            db,
            serialize_estimate_for_pdf(estimate_for_pdf),
            business_id=business_id,
        )
        estimate_pdf_path = generate_estimate(
            estimateId,
            estimate_pdf_payload,
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
    material_query = {"business_id": business_id} if business_id else {"_id": None}
    equipment_query = {"business_id": business_id} if business_id else {"_id": None}
    discount_query = {"business_id": business_id} if business_id else {"_id": None}
    category_query = {"company_id": str(business_id)} if business_id else {"_id": None}
    services = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
    parts = [_serialize_part_without_legacy_fields(part) for part in db.parts.find(part_query).sort("part_name", 1)]
    materials = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
    equipments = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
    discounts = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
    part_categories = [serialize_doc(category) for category in db.categories.find({**category_query, "type": "part"}).sort("name", 1)]
    material_categories = [serialize_doc(category) for category in db.categories.find({**category_query, "type": "material"}).sort("name", 1)]
    equipment_categories = [serialize_doc(category) for category in db.categories.find({**category_query, "type": "equipment"}).sort("name", 1)]
    employee_options = build_employee_options(db)
    services_catalog = build_service_catalog(services)
    parts_catalog = build_part_catalog(parts)
    materials_catalog = build_material_catalog(materials)
    equipments_catalog = build_equipment_catalog(equipments)
    discounts_catalog = build_discount_catalog(discounts)
    parts_by_id = {p["_id"]: p.get("part_name", "") for p in parts}
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
    initial_hvac_systems = _query_hvac_systems_for_property(db, estimate_doc.get("customer_id"), selected_property_id)
    job_services_hvac = [str(s.get("hvac_system_id") or "").strip() for s in (estimate_doc.get("services") or [])]
    job_parts_hvac = [str(p.get("hvac_system_id") or "").strip() for p in (estimate_doc.get("parts") or [])]
    job_labors_hvac = [str(l.get("hvac_system_id") or "").strip() for l in (estimate_doc.get("labors") or [])]
    job_materials_hvac = [str(m.get("hvac_system_id") or "").strip() for m in (estimate_doc.get("materials") or [])]
    job_equipments_hvac = [str(e.get("hvac_system_id") or "").strip() for e in (estimate_doc.get("equipments") or [])]

    return render_template(
        "estimates/update_estimate.html",
        estimateId=estimateId,
        estimate=estimate_doc,
        jobId=estimateId,
        job=estimate_doc,
        customer=customer,
        customer_properties=_get_customer_properties(customer),
        selected_property_id=selected_property_id,
        initial_hvac_systems=initial_hvac_systems,
        job_services_hvac=job_services_hvac,
        job_parts_hvac=job_parts_hvac,
        job_materials_hvac=job_materials_hvac,
        job_equipments_hvac=job_equipments_hvac,
        services=services,
        parts=parts,
        materials=materials,
        equipments=equipments,
        discounts=discounts,
        part_categories=part_categories,
        material_categories=material_categories,
        equipment_categories=equipment_categories,
        employee_options=employee_options,
        services_catalog=services_catalog,
        parts_catalog=parts_catalog,
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

        with open(filepath, "rb") as f:
            attachment_bytes = f.read()

        business = _resolve_business_doc_for_job(db, estimate) or {}
        send_email(
            subject=subject,
            recipients=[recipient_email],
            body=appended_body,
            attachments=[(filename, "application/pdf", attachment_bytes)],
            business=business,
        )

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
                "accepted_signature_captured_at": datetime.now(UTC),
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

    if estimate.get("payment_schedule"):
        fired_schedule, _ = _fire_payment_schedule_trigger(estimate.get("payment_schedule"), "estimate_accepted")
        db.estimates.update_one(
            {"_id": ObjectId(estimateId)},
            {"$set": {"payment_schedule": fired_schedule, "updated_at": datetime.now(UTC)}},
        )
        estimate["payment_schedule"] = fired_schedule

    try:
        created_job_id = _create_job_from_accepted_estimate(db, estimateId)
        if created_job_id:
            current_app.logger.info("Job auto-created from estimate acceptance: estimate_id=%s job_id=%s", estimateId, created_job_id)
    except Exception as exc:
        current_app.logger.error("Job auto-create failed for accepted estimate: estimate_id=%s error=%s", estimateId, exc)

    return redirect(_build_estimate_view_url(estimateId, access_token=token_value if not is_staff_view else ""))


@bp.route("/estimates/<estimateId>/payment-schedule/pay-later", methods=["POST"])
def send_estimate_payment_request_later(estimateId):
    db = ensure_connection_or_500()
    estimate = db.estimates.find_one({"_id": object_id_or_404(estimateId)})
    if not estimate:
        return redirect(url_for("home"))

    token_value = str(request.form.get("access_token") or request.args.get("token") or "").strip()
    is_staff_view = _is_authenticated_employee()
    if not is_staff_view and not _verify_estimate_access_token(estimate, token_value):
        return redirect(url_for("auth.login"))

    created_job_id = str(estimate.get("created_job_id") or "").strip()
    if not ObjectId.is_valid(created_job_id):
        return redirect(_build_estimate_view_url(estimateId, access_token=token_value if not is_staff_view else "", external=False))

    created_job = db.jobs.find_one({"_id": ObjectId(created_job_id)}) or {}
    stage_id = ""
    for stage in created_job.get("payment_schedule") or []:
        if not isinstance(stage, dict):
            continue
        if str(stage.get("trigger") or "").strip().lower() != "estimate_accepted":
            continue
        if str(stage.get("status") or "").strip().lower() not in {"due", "partial"}:
            continue
        stage_id = str(stage.get("stage_id") or "").strip()
        if stage_id:
            break

    if not stage_id:
        return redirect(
            url_for(
                "jobs.view_estimate",
                estimateId=estimateId,
                token=token_value if not is_staff_view else None,
                payment_request="failed",
                payment_request_reason="No due payment stage is available.",
            )
        )

    ok, detail = _send_payment_schedule_stage_request(db, created_job_id, stage_id, force_resend=False)
    return redirect(
        url_for(
            "jobs.view_estimate",
            estimateId=estimateId,
            token=token_value if not is_staff_view else None,
            payment_request="sent" if ok else "failed",
            payment_request_reason="" if ok else detail,
        )
    )


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

    invoice_status = _resolve_invoice_status(invoice, job_doc)
    invoice["status"] = invoice_status
    invoice_sent_display = _resolve_invoice_sent_display(invoice, job_doc)

    token_value = str(request.args.get("token") or "").strip()
    payment_state = str(request.args.get("payment") or "").strip().lower()
    payment_method_state = str(request.args.get("payment_method") or "").strip().lower()
    reminder_state = str(request.args.get("reminder") or "").strip().lower()
    payment_link_state = str(request.args.get("payment_link") or "").strip().lower()
    returned_session_id = str(request.args.get("session_id") or "").strip()
    is_staff_view = _is_authenticated_employee()
    has_customer_token = _verify_invoice_access_token(invoice, token_value)
    session_access_key = f"invoice_access_{jobId}_{invoiceRef}"
    has_customer_session_access = bool(session.get(session_access_key))
    if is_staff_view and not (has_customer_token or has_customer_session_access) and not _doc_belongs_to_current_business(db, job):
        return redirect(url_for("jobs.jobs"))
    if not is_staff_view and not (has_customer_token or has_customer_session_access):
        return redirect(url_for("auth.login"))

    # Persist short-lived access for the specific invoice view so follow-up actions
    # (like starting checkout) can succeed even if query token forwarding is brittle.
    if not is_staff_view and has_customer_token:
        session[session_access_key] = True

    # Refresh payment totals on load so displayed balances are always in sync.
    synchronized_job = _synchronize_job_payment_fields(db, jobId)
    if synchronized_job:
        job_doc = serialize_doc(synchronized_job)
        refreshed_invoice = _find_invoice_entry(job_doc, invoice_ref=invoiceRef)
        if refreshed_invoice:
            invoice = refreshed_invoice

    invoice_status = _resolve_invoice_status(invoice, job_doc)
    invoice["status"] = invoice_status
    invoice_sent_display = _resolve_invoice_sent_display(invoice, job_doc)
    invoice_sent_iso = _resolve_invoice_sent_iso(invoice, job_doc)
    invoice_status_normalized = invoice_status.lower()
    invoice_is_paid = invoice_status_normalized == "paid"
    payment_url = ""
    payment_url_display = ""
    invoice_qr_code = ""
    customer_qr_code = ""

    if not invoice_is_paid and token_value:
        payment_url = _build_invoice_view_url(jobId, invoiceRef, access_token=token_value, external=True)
        payment_url_display = _display_payment_link(payment_url)
        try:
            invoice_qr_code = generate_payment_qr(payment_url)
            customer_qr_code = invoice_qr_code
        except Exception as exc:
            current_app.logger.warning(
                "Invoice QR generation failed: job_id=%s invoice_ref=%s error=%s",
                jobId,
                invoiceRef,
                exc,
            )
            invoice_qr_code = ""
            customer_qr_code = ""

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

        invoice_status = _resolve_invoice_status(invoice, job_doc)
        invoice["status"] = invoice_status
        invoice_is_paid = invoice_status.lower() == "paid"

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
    due_date = str((invoice or {}).get("due_date") or "").strip()
    if completed_dt:
        due_date = due_date or (completed_dt + timedelta(days=payment_due_days)).strftime("%m/%d/%Y")

    stripe_connect_ready = False
    stripe_connect_reason = ""
    business = {}
    business_id = str(job_doc.get("business_id") or "").strip()
    if not business_id:
        # Fallback for older jobs saved before business_id was persisted.
        sole_business = db.businesses.find_one({}, {"_id": 1})
        if sole_business:
            business_id = str(sole_business["_id"])
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

    pricing_summary = _build_invoice_pricing_summary(job_doc, business_doc=business, customer_doc=customer)
    payment_history = _build_payment_history(db, jobId, _resolve_invoice_ref(invoice, fallback_ref=invoiceRef))
    payment_summary = _build_invoice_payment_summary(job_doc, pricing_summary, payment_history)
    total_amount_paid = round(_safe_float(payment_summary.get("amount_paid"), 0.0), 2)
    balance_due = round(_safe_float(payment_summary.get("invoice_balance"), 0.0), 2)
    payment_schedule_view = _build_payment_schedule_view(job_doc.get("payment_schedule") or [])
    payment_schedule_due_now = _payment_schedule_due_now_amount(job_doc.get("payment_schedule") or [])

    return render_template(
        "invoices/view_invoice.html",
        jobId=jobId,
        invoiceRef=invoiceRef,
        invoice=invoice,
        job=job_doc,
        customer=customer,
        pricing_summary=pricing_summary,
        payment_summary=payment_summary,
        payment_history=payment_history,
        total_amount_paid=total_amount_paid,
        balance_due=balance_due,
        payment_schedule_view=payment_schedule_view,
        payment_schedule_due_now=payment_schedule_due_now,
        payment_due_days=payment_due_days,
        due_date=due_date,
        invoice_sent_display=invoice_sent_display,
        invoice_sent_iso=invoice_sent_iso,
        is_staff_view=is_staff_view,
        access_token=token_value,
        invoice_email_template=invoice_email_template,
        company_name=_resolve_company_name(business),
        payment_state=payment_state,
        payment_method_state=payment_method_state,
        reminder_state=reminder_state,
        payment_link_state=payment_link_state,
        stripe_publishable_key=_resolve_stripe_publishable_key(),
        stripe_connect_ready=stripe_connect_ready,
        stripe_connect_reason=stripe_connect_reason,
        payment_url=payment_url,
        payment_url_display=payment_url_display,
        invoice_qr_code=invoice_qr_code,
        customer_qr_code=customer_qr_code,
    )


@bp.route("/jobs/<jobId>/invoices/<invoiceRef>/payment-link", methods=["POST"])
def generate_invoice_payment_link(jobId, invoiceRef):
    if not _is_authenticated_employee():
        return redirect(url_for("auth.login"))

    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    job_doc = serialize_doc(job)
    invoice = _find_invoice_entry(job_doc, invoice_ref=invoiceRef)
    if not invoice:
        return redirect(url_for("jobs.view_job", jobId=jobId))

    invoice_status = _resolve_invoice_status(invoice, job_doc)
    if str(invoice_status or "").strip().lower() == "paid":
        return redirect(url_for("jobs.view_invoice", jobId=jobId, invoiceRef=invoiceRef, payment_link="paid"))

    resolved_invoice_ref = _resolve_invoice_ref(invoice, fallback_ref=invoiceRef)
    access_token = _issue_invoice_access_token(db, jobId, resolved_invoice_ref)
    if not access_token:
        return redirect(url_for("jobs.view_invoice", jobId=jobId, invoiceRef=invoiceRef, payment_link="failed"))

    return redirect(
        url_for(
            "jobs.view_invoice",
            jobId=jobId,
            invoiceRef=invoiceRef,
            token=access_token,
            payment_link="generated",
        )
    )


@bp.route("/jobs/<jobId>/invoices/<invoiceRef>/stripe-checkout", methods=["POST"])
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
    token_value = str(request_data.get("access_token") or request.form.get("access_token") or request.form.get("token") or "").strip()
    is_staff_view = _is_authenticated_employee()
    session_access_key = f"invoice_access_{jobId}_{invoiceRef}"
    has_customer_session_access = bool(session.get(session_access_key))
    if not is_staff_view and not (_verify_invoice_access_token(invoice, token_value) or has_customer_session_access):
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    secret_key = _configure_stripe_client()
    if not secret_key:
        return jsonify({"success": False, "error": "Stripe is not configured"}), 500

    synchronized_job = _synchronize_job_payment_fields(db, jobId)
    if synchronized_job:
        job_doc = serialize_doc(synchronized_job)

    payment_state = _calculate_job_payment_state(db, job_doc)
    balance_due = payment_state["balance_due"]
    if balance_due <= 0:
        return jsonify({"success": False, "error": "Invoice is already fully paid"}), 400

    requested_amount = request_data.get("amount")
    if requested_amount in (None, ""):
        requested_amount = request.form.get("amount", "")
    charge_amount = round(_safe_float(requested_amount, balance_due), 2)
    if charge_amount <= 0 or charge_amount > balance_due:
        return jsonify({"success": False, "error": "Payment amount must be greater than zero and no more than balance due"}), 400

    amount_total = int(round(charge_amount * 100))

    business_id = str(job_doc.get("business_id") or "").strip()
    if not business_id:
        sole_business = db.businesses.find_one({}, {"_id": 1})
        if sole_business:
            business_id = str(sole_business["_id"])
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

    customer_doc = db.customers.find_one(build_reference_filter("_id", job.get("customer_id")), {"email": 1, "stripe_customer_id": 1}) or {}
    customer_email = str((request_data.get("customer_email") or request.form.get("customer_email") or "")).strip() or str((job_doc.get("email") or "")).strip() or str(customer_doc.get("email") or "").strip()
    stripe_customer_id = str(customer_doc.get("stripe_customer_id") or "").strip()

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
        payment_method_types=["card", "us_bank_account"],
        customer=stripe_customer_id or None,
        customer_email=None if stripe_customer_id else (customer_email or None),
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
            "amount": f"{charge_amount:.2f}",
        },
        payment_intent_data={
            "application_fee_amount": application_fee_amount,
            "transfer_data": {"destination": stripe_account_id},
            "metadata": {
                "job_id": jobId,
                "invoice_ref": str(invoice.get("invoice_id") or invoice.get("invoice_number") or invoiceRef),
                "business_id": business_id,
                "amount": f"{charge_amount:.2f}",
            },
        },
        success_url=success_url,
        cancel_url=cancel_url,
    )

    return jsonify({"success": True, "checkout_url": checkout_session.url}), 200


@bp.route("/jobs/<jobId>")
def view_job(jobId):
    db = ensure_connection_or_500()
    synchronized_job = _synchronize_job_payment_fields(db, jobId)
    job = synchronized_job or db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    if not _doc_belongs_to_current_business(db, job):
        return redirect(url_for("jobs.jobs"))

    payment_schedule_request_state = str(request.args.get("payment_schedule_request") or "").strip().lower()
    payment_schedule_request_reason = str(request.args.get("payment_schedule_reason") or "").strip()

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
    pending_hvac_system_prompt_job_id = str(session.get("pending_hvac_system_prompt_job_id") or "").strip()
    hvac_system_prompt_items = []
    if pending_hvac_system_prompt_job_id and pending_hvac_system_prompt_job_id == str(jobId):
        hvac_system_prompt_items = _build_hvac_system_prompt_items(job_doc)
        if not hvac_system_prompt_items:
            session.pop("pending_hvac_system_prompt_job_id", None)
    job_business = {}
    job_business_id = str(job_doc.get("business_id") or "").strip()
    if not job_business_id:
        sole_business = db.businesses.find_one({}, {"_id": 1})
        if sole_business:
            job_business_id = str(sole_business["_id"])
    if job_business_id:
        business_doc = db.businesses.find_one(build_reference_filter("_id", job_business_id))
        if business_doc:
            job_business = serialize_doc(business_doc)

    pricing_summary = _build_invoice_pricing_summary(job_doc, business_doc=job_business, customer_doc=customer)
    job_doc["internal_notes"] = _build_internal_notes_for_view(db, job)
    payment_history = _build_payment_history(db, jobId)
    payment_schedule_view = _build_payment_schedule_view(job_doc.get("payment_schedule") or [])

    maintenance_plan_discount = None
    if job_doc.get("plan_discount_applied") and job_doc.get("maintenance_plan_id"):
        plan_doc = db.maintenance_plans.find_one(build_reference_filter("_id", job_doc.get("maintenance_plan_id")))
        if plan_doc:
            plan_snapshot = plan_doc.get("template_snapshot") or {}
            plan_service_types = [
                str(item).strip().title()
                for item in (plan_snapshot.get("discount_service_types") or [])
                if str(item).strip()
            ]
            try:
                discount_pct_value = float(job_doc.get("plan_discount_pct") or 0)
            except (TypeError, ValueError):
                discount_pct_value = 0.0
            maintenance_plan_discount = {
                "plan_id": str(plan_doc.get("_id")),
                "plan_name": str(plan_snapshot.get("name") or "Maintenance Plan").strip() or "Maintenance Plan",
                "plan_number": str(plan_doc.get("plan_number") or "").strip(),
                "pct_display": f"{discount_pct_value:g}",
                "service_types": plan_service_types,
            }

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
        hvac_system_prompt_items=hvac_system_prompt_items,
        payment_history=payment_history,
        job_series=serialize_doc(job_series) if job_series else None,
        customer=customer,
        quote_email_template=quote_email_template,
        invoice_email_template=invoice_email_template,
        pricing_summary=pricing_summary,
        payment_schedule_view=payment_schedule_view,
        payment_schedule_request_state=payment_schedule_request_state,
        payment_schedule_request_reason=payment_schedule_request_reason,
        employee_has_other_active_job=employee_has_other_active_job,
        maintenance_plan_discount=maintenance_plan_discount,
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
    current_timestamp_utc = datetime.now(UTC)
    payment_schedule = job.get("payment_schedule") or []
    if payment_schedule:
        payment_schedule, _ = _fire_payment_schedule_trigger(payment_schedule, "job_started", current_timestamp_utc)

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {"$set": {"status": "Started", "dateStarted": current_timestamp, "started_at": current_timestamp_utc, "updated_at": current_timestamp_utc, "payment_schedule": payment_schedule}},
    )

    if payment_schedule:
        _sync_job_payment_schedule(db, jobId)
        _send_triggered_payment_schedule_requests(db, jobId, "job_started")

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

    current_timestamp_utc = datetime.now(UTC)

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {"$set": {"status": "En Route", "en_route_at": current_timestamp_utc, "updated_at": current_timestamp_utc}},
    )

    try:
        _send_en_route_sms_notification(db, job)
    except Exception as exc:
        current_app.logger.error("En-route SMS handler failed unexpectedly: job_id=%s error=%s", jobId, exc)

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
                "tax_rates": 1,
            },
        )
        if business_doc:
            business = serialize_doc(business_doc)

    business_logo_path = resolve_current_business_logo_path(db)
    invoice_pdf_payload = _hydrate_service_descriptions_for_pdf(
        db,
        job,
        business_id=job.get("business_id") or resolve_current_business_id(db),
    )
    invoice_path = generate_invoice(jobId, invoice_pdf_payload, customer, business_logo_path=business_logo_path, business=business)
    filename = os.path.basename(invoice_path)

    current_timestamp = datetime.now().strftime("%m/%d/%Y %H:%M:%S")
    current_timestamp_utc = datetime.now(UTC)
    time_spent_str = ""

    def _add_months_utc(base_dt, month_count):
        if month_count <= 0:
            return None

        target_year = base_dt.year + ((base_dt.month - 1 + month_count) // 12)
        target_month = ((base_dt.month - 1 + month_count) % 12) + 1
        target_day = min(base_dt.day, calendar.monthrange(target_year, target_month)[1])
        return base_dt.replace(year=target_year, month=target_month, day=target_day)

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

    completion_pricing_summary = _build_invoice_pricing_summary(serialize_doc(job), business_doc=business, customer_doc=customer)
    completion_total_due = round(_safe_float(completion_pricing_summary.get("total_due"), _safe_float(job.get("total_amount"), 0.0)), 2)
    existing_paid = round(_safe_float(job.get("total_amount_paid"), 0.0), 2)
    completion_balance_due = round(max(0.0, completion_total_due - existing_paid), 2)

    finalized_equipments = []
    for equipment in (job.get("equipments") or []):
        if not isinstance(equipment, dict):
            continue

        equipment_row = dict(equipment)
        warranty_months = int(_safe_float(equipment_row.get("warranty_months"), 0.0))
        warranty_expires_at = _add_months_utc(current_timestamp_utc, warranty_months)
        equipment_row["warranty_months"] = warranty_months if warranty_months > 0 else None
        equipment_row["warranty_expires"] = warranty_expires_at if warranty_expires_at else None
        finalized_equipments.append(equipment_row)

    total_parts_cost = 0.0
    for part in (job.get("parts") or []):
        if not isinstance(part, dict):
            continue
        part_cost = _safe_float(part.get("cost_price") or part.get("cost") or 0.0)
        if part_cost > 0:
            total_parts_cost += part_cost

    total_materials_cost = 0.0
    for material in (job.get("materials") or []):
        if not isinstance(material, dict):
            continue

        explicit_cost_total = _safe_float(material.get("cost_total") or 0.0)
        if explicit_cost_total > 0:
            total_materials_cost += explicit_cost_total
            continue

        quantity_value = _safe_float(material.get("quantity") or material.get("quantity_used") or 0.0)
        cost_per_unit = _safe_float(material.get("cost_price_per_unit") or 0.0)
        if quantity_value > 0 and cost_per_unit > 0:
            total_materials_cost += quantity_value * cost_per_unit
        else:
            current_app.logger.warning(
                "Skipping material cost rollup due to missing cost/quantity: job_id=%s material=%s",
                jobId,
                str(material.get("material_name") or material.get("description") or ""),
            )

    total_equipment_cost = 0.0
    for equipment in finalized_equipments:
        if not isinstance(equipment, dict):
            continue

        quantity_value = _safe_float(equipment.get("quantity_installed") or equipment.get("quantity") or 0.0)
        if quantity_value <= 0:
            quantity_value = 1.0

        equipment_cost = _safe_float(equipment.get("cost_price") or equipment.get("cost") or 0.0)
        if equipment_cost > 0:
            total_equipment_cost += quantity_value * equipment_cost

    total_parts_cost = round(total_parts_cost, 2)
    total_materials_cost = round(total_materials_cost, 2)
    total_equipment_cost = round(total_equipment_cost, 2)
    gross_profit = round(completion_total_due - total_parts_cost - total_materials_cost - total_equipment_cost, 2)

    if completion_balance_due <= 0:
        completion_payment_status = "paid"
    elif existing_paid <= 0:
        completion_payment_status = "pending_paid"
    else:
        completion_payment_status = "partial_paid"

    completion_job_status = "Paid" if completion_balance_due <= 0 else "Completed"
    payment_schedule = job.get("payment_schedule") or []
    if payment_schedule:
        payment_schedule, _ = _fire_payment_schedule_trigger(payment_schedule, "job_completed", current_timestamp_utc)

    existing_invoices = [entry for entry in (job.get("invoices") or []) if isinstance(entry, dict)]
    if existing_invoices:
        latest_invoice = dict(existing_invoices[-1])
        latest_invoice["invoice_number"] = str(latest_invoice.get("invoice_number") or _build_job_invoice_number(jobId)).strip() or _build_job_invoice_number(jobId)
        latest_invoice["file_path"] = url_for("download_invoice", filename=filename)
        latest_invoice["status"] = "Paid" if completion_balance_due <= 0 and existing_paid > 0 else "Created"
        latest_invoice["is_provisional"] = False
        existing_invoices[-1] = latest_invoice
    else:
        existing_invoices = [
            {
                "invoice_id": str(ObjectId()),
                "invoice_number": _build_job_invoice_number(jobId),
                "job_id": str(jobId),
                "file_path": url_for("download_invoice", filename=filename),
                "sent_at": None,
                "due_date": "",
                "status": "Paid" if completion_balance_due <= 0 and existing_paid > 0 else "Created",
                "is_provisional": False,
            }
        ]

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {
            "$set": {
                "status": completion_job_status,
                "dateCompleted": current_timestamp,
                "completed_at": current_timestamp_utc,
                "updated_at": current_timestamp_utc,
                "timeSpent": time_spent_str,
                "total_amount": completion_total_due,
                "total_amount_paid": existing_paid,
                "balance_due": completion_balance_due,
                "payment_status": completion_payment_status,
                "equipments": finalized_equipments,
                "total_parts_cost": total_parts_cost,
                "total_materials_cost": total_materials_cost,
                "total_equipment_cost": total_equipment_cost,
                "gross_profit": gross_profit,
                "payment_schedule": payment_schedule,
                "invoices": existing_invoices,
            },
        },
    )

    if payment_schedule:
        _sync_job_payment_schedule(db, jobId)
        _send_triggered_payment_schedule_requests(db, jobId, "job_completed")

    try:
        serviced_count = _mark_hvac_systems_serviced(db, job, current_timestamp_utc)
        if serviced_count:
            current_app.logger.info("Updated last_serviced metadata for %s HVAC systems on job_id=%s", serviced_count, jobId)
    except Exception as hvac_update_exc:
        current_app.logger.error("HVAC last_serviced update failed: job_id=%s error=%s", jobId, hvac_update_exc)

    remaining_unlinked_equipment_indexes = [
        index
        for index, equipment in enumerate(finalized_equipments)
        if isinstance(equipment, dict) and not str(equipment.get("hvac_system_id") or "").strip()
    ]
    if remaining_unlinked_equipment_indexes:
        session["pending_hvac_system_prompt_job_id"] = str(jobId)
    else:
        session.pop("pending_hvac_system_prompt_job_id", None)

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
        _refresh_customer_balance_from_jobs(db, customer_oid)

    return redirect(next_url if next_url else url_for("jobs.view_job", jobId=jobId))


@bp.route("/jobs/<jobId>/equipment-hvac-systems", methods=["POST"])
def create_hvac_systems_from_job_equipment(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    prompt_job_id = str(session.get("pending_hvac_system_prompt_job_id") or "").strip()
    if prompt_job_id and prompt_job_id != str(jobId):
        session.pop("pending_hvac_system_prompt_job_id", None)

    if str(request.form.get("skip_hvac_system_prompt") or "").strip():
        session.pop("pending_hvac_system_prompt_job_id", None)
        return redirect(url_for("jobs.view_job", jobId=jobId))

    equipment_rows = list(job.get("equipments") or [])
    selected_indexes = {
        str(index).strip()
        for index in request.form.getlist("create_hvac_system[]")
        if str(index or "").strip()
    }
    if not selected_indexes:
        session.pop("pending_hvac_system_prompt_job_id", None)
        return redirect(url_for("jobs.view_job", jobId=jobId))

    updated_equipment_rows = []
    created_count = 0
    for index, equipment in enumerate(equipment_rows):
        if not isinstance(equipment, dict):
            updated_equipment_rows.append(equipment)
            continue

        equipment_copy = dict(equipment)
        index_text = str(index)
        if index_text not in selected_indexes:
            updated_equipment_rows.append(equipment_copy)
            continue

        serial_field_name = f"equipment_serial_number_{index}"
        entered_serial_number = str(request.form.get(serial_field_name) or equipment_copy.get("serial_number") or "").strip()
        equipment_copy["serial_number"] = entered_serial_number
        equipment_copy["source_job_equipment_index"] = index

        creation_payload = _build_hvac_system_creation_payload_from_job_equipment(job, equipment_copy, entered_serial_number)
        if not creation_payload:
            updated_equipment_rows.append(equipment_copy)
            continue

        inserted_system = db.hvacSystems.insert_one(creation_payload)
        system_id = str(inserted_system.inserted_id)
        system_name = str(creation_payload.get("system_nickname") or "HVAC System").strip() or "HVAC System"
        system_type = str(creation_payload.get("system_type") or "HVAC System").strip() or "HVAC System"
        equipment_copy["hvac_system_id"] = system_id
        equipment_copy["hvac_system_name"] = f"{system_type} - {system_name}" if system_name else system_type
        created_count += 1
        updated_equipment_rows.append(equipment_copy)

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {
            "$set": {
                "equipments": updated_equipment_rows,
                "updated_at": datetime.now(UTC),
            }
        },
    )

    remaining_unlinked = [
        equipment_row
        for equipment_row in updated_equipment_rows
        if isinstance(equipment_row, dict) and not str(equipment_row.get("hvac_system_id") or "").strip()
    ]
    if remaining_unlinked:
        session["pending_hvac_system_prompt_job_id"] = str(jobId)
    else:
        session.pop("pending_hvac_system_prompt_job_id", None)

    current_app.logger.info(
        "Created HVAC systems from completed job equipment: job_id=%s created_count=%s by employee_id=%s",
        jobId,
        created_count,
        session.get("employee_id"),
    )

    return redirect(url_for("jobs.view_job", jobId=jobId))


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

        filepath = ""
        filename = ""

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

                    # Rebuild invoice PDF with the latest tokenized payment URL so
                    # attached invoices include a scannable payment QR code.
                    try:
                        customer_for_pdf = {}
                        customer_ref = job.get("customer_id")
                        if customer_ref:
                            customer_doc = db.customers.find_one(build_reference_filter("_id", customer_ref))
                            if customer_doc:
                                customer_for_pdf = serialize_doc(customer_doc)

                        business_for_pdf = {}
                        business_id = str(job.get("business_id") or "").strip()
                        if not business_id:
                            sole_business = db.businesses.find_one({}, {"_id": 1})
                            if sole_business:
                                business_id = str(sole_business.get("_id") or "").strip()
                        if business_id:
                            business_doc = db.businesses.find_one(
                                build_reference_filter("_id", business_id),
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
                                    "tax_rates": 1,
                                },
                            )
                            if business_doc:
                                business_for_pdf = serialize_doc(business_doc)

                        business_logo_path = resolve_current_business_logo_path(db)
                        invoice_pdf_payload = _hydrate_service_descriptions_for_pdf(
                            db,
                            job,
                            business_id=job.get("business_id") or resolve_current_business_id(db),
                        )
                        regenerated_invoice_path = generate_invoice(
                            jobId,
                            invoice_pdf_payload,
                            customer_for_pdf,
                            business_logo_path=business_logo_path,
                            business=business_for_pdf,
                            payment_url=invoice_link,
                        )
                        filename = os.path.basename(regenerated_invoice_path)
                        filepath = regenerated_invoice_path
                        estimate_file = url_for("download_invoice", filename=filename)
                    except Exception as exc:
                        current_app.logger.warning(
                            "Invoice PDF regeneration with payment QR failed: job_id=%s invoice_ref=%s error=%s",
                            jobId,
                            invoice_ref,
                            exc,
                        )

        if not filepath:
            filepath = estimate_pdf_absolute_path_from_url(estimate_file)

        if not filepath or not os.path.exists(filepath):
            if email_type == "invoice":
                return jsonify({"success": False, "error": "Invoice file not found"}), 404
            return jsonify({"success": False, "error": "Estimate file not found"}), 404

        if not filename:
            filename = os.path.basename(filepath)

        with open(filepath, "rb") as f:
            attachment_bytes = f.read()

        business = _resolve_business_doc_for_job(db, job) or {}
        send_email(
            subject=subject,
            recipients=[recipient_email],
            body=appended_body,
            attachments=[(filename, "application/pdf", attachment_bytes)],
            business=business,
        )

        if email_type == "invoice":
            sent_at_utc = datetime.now(UTC)
            sent_at_text = sent_at_utc.strftime("%m/%d/%Y %H:%M:%S")
            invoice_ref = str(
                (resolved_invoice_entry or {}).get("invoice_id")
                or (resolved_invoice_entry or {}).get("invoice_number")
                or ""
            ).strip()

            refreshed_job = db.jobs.find_one({"_id": ObjectId(jobId)})
            refreshed_job_doc = serialize_doc(refreshed_job) if refreshed_job else {}
            refreshed_invoice_entry = _find_invoice_entry(refreshed_job_doc, invoice_ref=invoice_ref) if invoice_ref else None
            completed_dt = _parse_mmddyyyy_date(str((refreshed_job_doc or {}).get("dateCompleted") or "")[:10])
            payment_due_days = _normalize_payment_due_days((refreshed_job_doc or {}).get("payment_due_days"), 30)
            due_date = (completed_dt + timedelta(days=payment_due_days)).strftime("%m/%d/%Y") if completed_dt else ""

            updated_invoices = []
            for entry in list(refreshed_job_doc.get("invoices") or []):
                if not isinstance(entry, dict):
                    updated_invoices.append(entry)
                    continue

                entry_id = str(entry.get("invoice_id") or "").strip()
                entry_number = str(entry.get("invoice_number") or "").strip()
                updated_entry = _sanitize_invoice_payment_fields(entry)
                if invoice_ref and invoice_ref in {entry_id, entry_number}:
                    updated_entry["sent_at"] = sent_at_utc.isoformat()
                    updated_entry["due_date"] = due_date
                    updated_entry["status"] = "Sent"
                    if estimate_file:
                        updated_entry["file_path"] = estimate_file
                updated_invoices.append(updated_entry)

            db.jobs.update_one(
                {"_id": ObjectId(jobId)},
                {
                    "$set": {
                        "date_invoice_sent": sent_at_text,
                        "invoices": updated_invoices,
                        "updated_at": sent_at_utc,
                    }
                },
            )

            if refreshed_job_doc and refreshed_invoice_entry:
                schedule_invoice_reminders_for_invoice(
                    db,
                    refreshed_job_doc,
                    refreshed_invoice_entry,
                    sent_at=sent_at_utc,
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

    if not _doc_belongs_to_current_business(db, job):
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
        selected_material_names = request.form.getlist("material_name[]")
        entered_material_quantities = request.form.getlist("material_quantity_used[]")
        entered_material_units = request.form.getlist("material_unit_of_measure[]")
        entered_material_prices = request.form.getlist("material_price[]")
        selected_equipment_names = request.form.getlist("equipment_name[]")
        entered_equipment_quantities = request.form.getlist("equipment_quantity_installed[]")
        entered_equipment_prices = request.form.getlist("equipment_price[]")
        entered_equipment_serial_numbers = request.form.getlist("equipment_serial_number[]")
        selected_discount_names = request.form.getlist("discount_name[]")
        entered_discount_percentages = request.form.getlist("discount_percentage[]")
        entered_discount_amounts = request.form.getlist("discount_amount[]")
        service_query = {"business_id": business_id} if business_id else {"_id": None}
        part_query = {"business_id": business_id} if business_id else {"_id": None}
        material_query = {"business_id": business_id} if business_id else {"_id": None}
        equipment_query = {"business_id": business_id} if business_id else {"_id": None}
        discount_query = {"business_id": business_id} if business_id else {"_id": None}
        service_docs = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
        part_docs = [_serialize_part_without_legacy_fields(part) for part in db.parts.find(part_query).sort("part_name", 1)]
        material_docs = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
        equipment_docs = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
        discount_docs = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
        service_catalog = build_service_catalog(service_docs)
        part_catalog = build_part_catalog(part_docs)
        material_catalog = build_material_catalog(material_docs)
        equipment_catalog = build_equipment_catalog(equipment_docs)
        discount_catalog = build_discount_catalog(discount_docs)
        business_doc_for_rates = serialize_doc(db.businesses.find_one({"_id": business_id})) if business_id else {}
        services, services_total = build_job_services_from_form(
            selected_service_types,
            entered_service_prices,
            entered_service_durations,
            service_catalog,
            business_doc_for_rates.get("labor_rate_standard"),
            entered_service_emergency_calls,
        )
        parts, parts_total = build_job_parts_from_form(
            selected_part_names,
            entered_part_prices,
            part_catalog,
        )
        labors = []
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
            entered_equipment_serial_numbers,
            equipment_catalog,
        )
        discounts, discounts_total = build_job_discounts_from_form(
            selected_discount_names,
            entered_discount_percentages,
            entered_discount_amounts,
            discount_catalog,
        )
        hvac_lookup = _build_hvac_system_lookup_for_property(db, job.get("customer_id"), selected_property_id)
        _apply_hvac_tags_to_components(services, request.form.getlist("service_hvac_system_id[]"), hvac_lookup)
        _apply_hvac_tags_to_components(parts, request.form.getlist("part_hvac_system_id[]"), hvac_lookup)
        _apply_hvac_tags_to_components(materials, request.form.getlist("material_hvac_system_id[]"), hvac_lookup)
        _apply_hvac_tags_to_components(equipments, request.form.getlist("equipment_hvac_system_id[]"), hvac_lookup)
        customer = {}
        customer_id = job.get("customer_id")
        if customer_id:
            customer_doc = db.customers.find_one(build_reference_filter("_id", customer_id))
            if customer_doc:
                customer = serialize_doc(customer_doc)

        pricing_summary = _build_pricing_summary(
            {
                "services": services,
                "parts": parts,
                "labors": labors,
                "materials": materials,
                "equipments": equipments,
                "discounts": discounts,
            },
            business_doc=business_doc_for_rates,
            customer_doc=customer,
        )
        total = pricing_summary["total_due"]
        payment_schedule = _build_payment_schedule_for_record(
            {"services": services, "payment_schedule": job.get("payment_schedule") or []},
            business_doc=business_doc_for_rates,
            total_amount=total,
            raw_schedule=request.form.get("payment_schedule_json", ""),
        )

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
        primary_technician_id = _resolve_employee_id_value(db, request.form.get("primary_technician_id", "") or request.form.get("job_assigned_employee", ""))
        technician_payload = _build_job_technician_payload(
            db,
            primary_technician_id,
            request.form.getlist("additional_technician_ids[]"),
        )
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
            primary_technician_id=technician_payload.get("primary_technician_id") or "",
        )

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
            "primary_technician_id": technician_payload.get("primary_technician_id") or None,
            "additional_technician_ids": technician_payload.get("additional_technician_ids") or [],
            "additional_technician_names": technician_payload.get("additional_technician_names") or [],
            "assigned_employee": technician_payload.get("assigned_employee") or "",
            "invoice_notes": request.form.get("invoice_notes", "").strip(),
            "payment_due_days": payment_due_days,
            "total_amount": float(total or 0.0),
            "payment_schedule": payment_schedule,
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
                    "primary_technician_id": technician_payload.get("primary_technician_id") or None,
                    "additional_technician_ids": technician_payload.get("additional_technician_ids") or [],
                    "additional_technician_names": technician_payload.get("additional_technician_names") or [],
                    "assigned_employee": technician_payload.get("assigned_employee") or "",
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
                    technician_payload,
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

        # The plan savings are stored as a discount row that is recomputed from
        # the current line items on every save (the row is stripped and rebuilt
        # inside apply_plan_discount), so it is always safe to re-apply.
        active_plan = get_active_plan_for_property(db, update_data.get("property_id"), business_id)
        if active_plan:
            update_data = apply_plan_discount(
                update_data,
                active_plan,
                business_doc=business_doc_for_rates,
                customer_doc=customer,
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
    material_query = {"business_id": business_id} if business_id else {"_id": None}
    equipment_query = {"business_id": business_id} if business_id else {"_id": None}
    discount_query = {"business_id": business_id} if business_id else {"_id": None}
    category_query = {"company_id": str(business_id)} if business_id else {"_id": None}
    services = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
    parts = [_serialize_part_without_legacy_fields(part) for part in db.parts.find(part_query).sort("part_name", 1)]
    materials = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
    equipments = [serialize_doc(equipment) for equipment in db.equipment.find(equipment_query).sort("equipment_name", 1)]
    discounts = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
    part_categories = [serialize_doc(category) for category in db.categories.find({**category_query, "type": "part"}).sort("name", 1)]
    material_categories = [serialize_doc(category) for category in db.categories.find({**category_query, "type": "material"}).sort("name", 1)]
    equipment_categories = [serialize_doc(category) for category in db.categories.find({**category_query, "type": "equipment"}).sort("name", 1)]
    employee_options = build_employee_options(db)
    services_catalog = build_service_catalog(services)
    parts_catalog = build_part_catalog(parts)
    materials_catalog = build_material_catalog(materials)
    equipments_catalog = build_equipment_catalog(equipments)
    discounts_catalog = build_discount_catalog(discounts)
    parts_by_id = {p["_id"]: p.get("part_name", "") for p in parts}
    materials_by_id = {m["_id"]: m["material_name"] for m in materials}
    default_payment_due_days = _resolve_default_payment_due_days(db)
    payment_due_days_value = _normalize_payment_due_days(job.get("payment_due_days"), default_payment_due_days)
    selected_property_id = str(job.get("property_id") or "").strip()
    if not selected_property_id:
        default_property = _resolve_default_property(customer)
        selected_property_id = str((default_property or {}).get("property_id") or "").strip()

    job_doc = serialize_doc(job)
    initial_hvac_systems = _query_hvac_systems_for_property(db, reference_value(job.get("customer_id")), selected_property_id)
    job_services_hvac = [str(s.get("hvac_system_id") or "").strip() for s in (job_doc.get("services") or [])]
    job_parts_hvac = [str(p.get("hvac_system_id") or "").strip() for p in (job_doc.get("parts") or [])]
    job_labors_hvac = [str(l.get("hvac_system_id") or "").strip() for l in (job_doc.get("labors") or [])]
    job_materials_hvac = [str(m.get("hvac_system_id") or "").strip() for m in (job_doc.get("materials") or [])]
    job_equipments_hvac = [str(e.get("hvac_system_id") or "").strip() for e in (job_doc.get("equipments") or [])]

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
        job_materials_hvac=job_materials_hvac,
        job_equipments_hvac=job_equipments_hvac,
        services=services,
        parts=parts,
        materials=materials,
        equipments=equipments,
        discounts=discounts,
        part_categories=part_categories,
        material_categories=material_categories,
        equipment_categories=equipment_categories,
        employee_options=employee_options,
        services_catalog=services_catalog,
        parts_catalog=parts_catalog,
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
