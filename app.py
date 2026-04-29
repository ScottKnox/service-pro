from datetime import datetime
import logging
import os
import re

from bson import ObjectId
from flask import Flask, redirect, render_template, request, send_file, session, url_for
from flask_mail import Mail
from flask_wtf.csrf import CSRFProtect

from blueprints import register_blueprints
from mongo import build_reference_filter, ensure_connection_or_500, serialize_doc
app = Flask(__name__)

# Session Configuration
_secret_key = os.getenv("SECRET_KEY")
if not _secret_key:
    raise RuntimeError("SECRET_KEY environment variable is not set")
app.secret_key = _secret_key

# Flask-Mail Configuration
app.config["MAIL_SERVER"] = os.getenv("MAIL_SERVER", "smtp.gmail.com")
app.config["MAIL_PORT"] = int(os.getenv("MAIL_PORT", 587))
app.config["MAIL_USE_TLS"] = os.getenv("MAIL_USE_TLS", "true").lower() == "true"
app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME")
app.config["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD")
app.config["MAIL_DEFAULT_SENDER"] = os.getenv("MAIL_DEFAULT_SENDER")

mail = Mail(app)
csrf = CSRFProtect(app)
register_blueprints(app)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", error_message="The page you requested could not be found."), 404


@app.errorhandler(500)
def internal_error(e):
    app.logger.error("Internal server error: %s", e)
    return render_template("error.html", error_message="An internal server error occurred. Please try again later."), 500


@app.before_request
def require_login():
    """Redirect unauthenticated users to login for all protected endpoints."""
    open_endpoints = {"auth.login", "auth.logout", "static", "home", "error_page"}
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
    db = ensure_connection_or_500()
    is_logged_in = bool(session.get("employee_id"))

    if not is_logged_in:
        return render_template("index.html", is_logged_in=False)

    current_employee_name = (session.get("employee_name") or "").strip()
    current_employee_position = (session.get("employee_position") or "").strip().lower()
    normalized_current_employee_name = " ".join(current_employee_name.lower().split())

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
            action_word = "marked paid for"
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
            "status": str((job_doc or {}).get("status") or "").strip(),
            "job_id": str((job_doc or {}).get("_id") or "").strip(),
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


@app.route("/invoices/<filename>")
def download_invoice(filename):
    """Serve invoice PDFs from the invoices directory."""
    invoices_dir = os.path.join(os.path.dirname(__file__), "invoices")
    filepath = os.path.join(invoices_dir, filename)

    if os.path.exists(filepath) and os.path.abspath(filepath).startswith(os.path.abspath(invoices_dir)):
        return send_file(filepath, mimetype="application/pdf", as_attachment=False)
    return "Invoice not found", 404


if __name__ == "__main__":
    app.run(debug=True)
