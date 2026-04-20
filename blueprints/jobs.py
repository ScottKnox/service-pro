from datetime import datetime
import json
import os

from bson import ObjectId
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for
from flask_mail import Message

from invoice_generator import generate_estimate, generate_invoice
from mongo import build_reference_filter, ensure_connection_or_500, object_id_or_404, reference_value, serialize_doc
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
from utils.formatters import format_date

bp = Blueprint("jobs", __name__)


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


@bp.route("/jobs")
def jobs():
    db = ensure_connection_or_500()
    jobs_list = [
        serialize_doc(job)
        for job in db.jobs.find().sort([("scheduled_at", 1), ("created_at", -1), ("_id", -1)])
    ]
    return render_template("jobs/jobs.html", jobs=jobs_list)


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


@bp.route("/customers/<customerId>/jobs/create", methods=["GET", "POST"])
def create_job(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    if request.method == "POST":
        business_id = resolve_current_business_id(db)
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
        
        # Initialize notes collection with the first note if provided
        notes_collection = []
        initial_note = request.form.get("job_notes", "").strip()
        if initial_note:
            notes_collection.append({
                "text": initial_note,
                "date": datetime.now().strftime("%m/%d/%Y %H:%M:%S")
            })
        
        new_job = {
            "customer_id": reference_value(customerId),
            "customer_name": f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip(),
            "company": customer.get("company", ""),
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
            "assigned_employee": assigned_employee,
            "total": f"${total:.2f}" if total else "$0.00",
            "total_amount": float(total or 0.0),
            "notes": notes_collection,
            "date_created": datetime.now().strftime("%m/%d/%Y"),
            "created_at": datetime.utcnow(),
            "scheduled_at": _combine_scheduled_datetime(scheduled_date, scheduled_time),
            "completed_at": None,
            "invoices": [],
            "business_id": business_id,
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

    return render_template(
        "jobs/create_job.html",
        customerId=customerId,
        customer=serialize_doc(customer),
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


@bp.route("/customers/<customerId>/estimates/create", methods=["GET", "POST"])
def create_estimate(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    if request.method == "POST":
        business_id = resolve_current_business_id(db)
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
        estimate_notes = request.form.get("estimate_notes", "").strip() or request.form.get("job_notes", "").strip()

        new_estimate = {
            "customer_id": reference_value(customerId),
            "customer_name": f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip(),
            "company": customer.get("company", ""),
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
            "created_by_employee": (session.get("employee_name") or "").strip(),
            "estimated_by_employee": estimated_by_employee,
            "total": f"${total:.2f}" if total else "$0.00",
            "total_amount": float(total or 0.0),
            "estimate_notes": estimate_notes,
            "file_path": [],
            "latest_file_path": "",
            "created_at": datetime.utcnow(),
            "sent_at": None,
            "accepted_at": None,
            "declined_at": None,
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

    return render_template(
        "estimates/create_estimate.html",
        customerId=customerId,
        customer=serialize_doc(customer),
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
        estimate_notes = request.form.get("estimate_notes", "").strip() or request.form.get("job_notes", "").strip()

        updated_data = {
            "services": services,
            "parts": parts,
            "labors": labors,
            "materials": materials,
            "equipments": equipments,
            "discounts": discounts,
            "address_line_1": request.form.get("job_address_line_1", "").strip(),
            "address_line_2": request.form.get("job_address_line_2", "").strip(),
            "city": request.form.get("job_city", "").strip(),
            "state": request.form.get("job_state", "").strip().upper(),
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

    return render_template(
        "estimates/update_estimate.html",
        estimateId=estimateId,
        estimate=estimate_doc,
        jobId=estimateId,
        job=estimate_doc,
        customer=customer,
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
            "sent_at": now,
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
                "accepted_at": now,
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
                "declined_at": now,
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

    return render_template(
        "jobs/view_job.html",
        jobId=jobId,
        job=serialize_doc(job),
        customer=customer,
        quote_email_template=quote_email_template,
        invoice_email_template=invoice_email_template,
    )


@bp.route("/jobs/<jobId>/start", methods=["POST"])
def start_job(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    current_status = str(job.get("status") or "").strip().lower()
    has_schedule = bool(str(job.get("scheduled_date") or "").strip()) and bool(str(job.get("scheduled_time") or "").strip())
    if current_status != "scheduled" or not has_schedule:
        current_app.logger.warning("Blocked invalid job start: job_id=%s status=%s has_schedule=%s", jobId, current_status, has_schedule)
        return redirect(url_for("jobs.view_job", jobId=jobId))

    current_timestamp = datetime.now().strftime("%m/%d/%Y %H:%M:%S")
    started_at = datetime.utcnow()

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {"$set": {"status": "Started", "dateStarted": current_timestamp, "started_at": started_at}},
    )

    return redirect(url_for("jobs.view_job", jobId=jobId))


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
                    "$set": {"status": "Completed", "dateCompleted": current_timestamp, "timeSpent": time_spent_str, "completed_at": datetime.utcnow()},
            "$push": {
                "invoices": {
                    "invoice_number": f"INV-{jobId[:8].upper()}",
                    "file_path": url_for("download_invoice", filename=filename),
                }
            },
        },
    )
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

    return redirect(url_for("jobs.view_job", jobId=jobId))


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

    if request.method == "POST":
        business_id = resolve_current_business_id(db)
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

        # Prepare notes update - add new note to collection if provided
        update_data = {
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
            "assigned_employee": assigned_employee,
            "total": f"${total:.2f}" if total else "$0.00",
            "total_amount": float(total or 0.0),
            "updated_at": datetime.utcnow(),
            "scheduled_at": _combine_scheduled_datetime(scheduled_date, scheduled_time),
        }

        new_note_text = request.form.get("job_notes", "").strip()
        if new_note_text:
            new_note = {
                "text": new_note_text,
                "date": datetime.now().strftime("%m/%d/%Y %H:%M:%S")
            }
            db.jobs.update_one(
                {"_id": ObjectId(jobId)},
                {
                    "$set": update_data,
                    "$push": {"notes": new_note}
                },
            )
        else:
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

    return render_template(
        "jobs/update_job.html",
        jobId=jobId,
        job=serialize_doc(job),
        customer=customer,
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


@bp.route("/jobs/<jobId>/delete", methods=["POST"])
def delete_job(jobId):
    db = ensure_connection_or_500()
    db.estimates.delete_many({"job_id": jobId})
    db.jobs.delete_one({"_id": object_id_or_404(jobId)})
    current_app.logger.info("Job deleted: id=%s by employee_id=%s", jobId, session.get("employee_id"))
    return redirect(url_for("jobs.jobs"))
