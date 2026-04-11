from datetime import datetime
import json
import os

from bson import ObjectId
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for
from flask_mail import Message

from invoice_generator import generate_invoice, generate_quote
from mongo import ensure_connection_or_500, object_id_or_404, serialize_doc
from utils.catalog import (
    build_discount_catalog,
    build_job_discounts_from_form,
    build_job_labors_from_form,
    build_job_materials_from_form,
    build_job_parts_from_form,
    build_job_services_from_form,
    build_labor_catalog,
    build_material_catalog,
    build_part_catalog,
    build_service_catalog,
)
from utils.currency import currency_to_float, normalize_currency
from utils.formatters import format_date

bp = Blueprint("jobs", __name__)


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


def resolve_job_status(scheduled_date, scheduled_time, services, parts, labors, materials, discounts, existing_status=""):
    """Derive job status from scheduling and line items while preserving terminal states."""
    normalized_existing = str(existing_status or "").strip().lower()
    if normalized_existing in {"started", "completed"}:
        return str(existing_status)

    has_schedule = bool(str(scheduled_date).strip()) and bool(str(scheduled_time).strip())
    has_line_items = bool(services or parts or labors or materials or discounts)

    if has_schedule and has_line_items:
        return "Scheduled"
    if has_schedule:
        return "Scheduled"
    if has_line_items:
        return "Estimating"
    return "Pending"


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
        for job in db.jobs.find().sort([("scheduled_date", 1), ("scheduled_time", 1), ("date_created", -1)])
    ]
    return render_template("jobs/jobs.html", jobs=jobs_list)


@bp.route("/customers/<customerId>/jobs/create", methods=["GET", "POST"])
def create_job(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    if request.method == "POST":
        business_id = resolve_current_business_id(db)
        selected_service_types = request.form.getlist("service_code[]") or request.form.getlist("service_type[]")
        entered_service_prices = request.form.getlist("service_standard_price[]") or request.form.getlist("service_price[]")
        entered_service_durations = request.form.getlist("service_estimated_hours[]") or request.form.getlist("service_duration[]")
        selected_part_names = request.form.getlist("part_code[]") or request.form.getlist("part_name[]")
        entered_part_prices = request.form.getlist("part_unit_cost[]") or request.form.getlist("part_price[]")
        selected_labor_descriptions = request.form.getlist("labor_description[]")
        entered_labor_hours = request.form.getlist("labor_hours[]")
        entered_labor_rates = request.form.getlist("labor_hourly_rate[]")
        selected_material_names = request.form.getlist("material_name[]")
        entered_material_quantities = request.form.getlist("material_quantity_used[]")
        entered_material_units = request.form.getlist("material_unit_of_measure[]")
        entered_material_prices = request.form.getlist("material_price[]")
        selected_discount_names = request.form.getlist("discount_name[]")
        entered_discount_percentages = request.form.getlist("discount_percentage[]")
        entered_discount_amounts = request.form.getlist("discount_amount[]")
        service_query = {"business_id": business_id} if business_id else {"_id": None}
        part_query = {"business_id": business_id} if business_id else {"_id": None}
        labor_query = {"business_id": business_id} if business_id else {"_id": None}
        material_query = {"business_id": business_id} if business_id else {"_id": None}
        discount_query = {"business_id": business_id} if business_id else {"_id": None}
        service_docs = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
        part_docs = [serialize_doc(part) for part in db.parts.find(part_query).sort("part_name", 1)]
        labor_docs = [serialize_doc(labor) for labor in db.labors.find(labor_query).sort("labor_description", 1)]
        material_docs = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
        discount_docs = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
        service_catalog = build_service_catalog(service_docs)
        part_catalog = build_part_catalog(part_docs)
        labor_catalog = build_labor_catalog(labor_docs)
        material_catalog = build_material_catalog(material_docs)
        discount_catalog = build_discount_catalog(discount_docs)
        services, services_total = build_job_services_from_form(
            selected_service_types,
            entered_service_prices,
            entered_service_durations,
            service_catalog,
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
        discounts, discounts_total = build_job_discounts_from_form(
            selected_discount_names,
            entered_discount_percentages,
            entered_discount_amounts,
            discount_catalog,
        )
        total = services_total + parts_total + labor_total + materials_total - discounts_total

        primary_service = services[0]["type"] if services else "No services added."
        scheduled_date = format_date(request.form.get("job_date", ""))
        scheduled_time = request.form.get("job_time", "").strip()
        job_status = resolve_job_status(scheduled_date, scheduled_time, services, parts, labors, materials, discounts)

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
            "customer_id": customerId,
            "customer_name": f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip(),
            "company": customer.get("company", ""),
            "job_type": primary_service,
            "services": services,
            "parts": parts,
            "labors": labors,
            "materials": materials,
            "discounts": discounts,
            "status": job_status,
            "scheduled_date": scheduled_date,
            "scheduled_time": scheduled_time,
            "address_line_1": request.form.get("job_address_line_1", "").strip(),
            "address_line_2": request.form.get("job_address_line_2", "").strip(),
            "city": request.form.get("job_city", "").strip(),
            "state": request.form.get("job_state", "").strip().upper(),
            "assigned_employee": assigned_employee,
            "total": f"${total:.2f}" if total else "$0.00",
            "notes": notes_collection,
            "date_created": datetime.now().strftime("%m/%d/%Y"),
            "invoices": [],
        }
        inserted = db.jobs.insert_one(new_job)
        current_app.logger.info("Job created: id=%s customer_id=%s by employee_id=%s", str(inserted.inserted_id), customerId, session.get("employee_id"))
        return redirect(url_for("jobs.view_job", jobId=str(inserted.inserted_id)))

    business_id = resolve_current_business_id(db)
    service_query = {"business_id": business_id} if business_id else {"_id": None}
    part_query = {"business_id": business_id} if business_id else {"_id": None}
    labor_query = {"business_id": business_id} if business_id else {"_id": None}
    material_query = {"business_id": business_id} if business_id else {"_id": None}
    discount_query = {"business_id": business_id} if business_id else {"_id": None}
    services = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
    parts = [serialize_doc(part) for part in db.parts.find(part_query).sort("part_name", 1)]
    labors = [serialize_doc(labor) for labor in db.labors.find(labor_query).sort("labor_description", 1)]
    materials = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
    discounts = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
    employee_options = build_employee_options(db)
    services_catalog = build_service_catalog(services)
    parts_catalog = build_part_catalog(parts)
    labors_catalog = build_labor_catalog(labors)
    materials_catalog = build_material_catalog(materials)
    discounts_catalog = build_discount_catalog(discounts)
    parts_by_id = {p["_id"]: p["part_code"] for p in parts}

    return render_template(
        "jobs/create_job.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        services=services,
        parts=parts,
        labors=labors,
        materials=materials,
        discounts=discounts,
        employee_options=employee_options,
        services_catalog=services_catalog,
        parts_catalog=parts_catalog,
        labors_catalog=labors_catalog,
        materials_catalog=materials_catalog,
        discounts_catalog=discounts_catalog,
        parts_by_id=parts_by_id,
    )


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
    if customer_id and ObjectId.is_valid(customer_id):
        customer_doc = db.customers.find_one({"_id": ObjectId(customer_id)})
        if customer_doc:
            customer = serialize_doc(customer_doc)

    estimates_list = db.estimates.find({"job_id": jobId}).sort([("date", -1)])
    estimates = [serialize_doc(estimate) for estimate in estimates_list]

    return render_template(
        "jobs/view_job.html",
        jobId=jobId,
        job=serialize_doc(job),
        customer=customer,
        estimates=estimates,
        quote_email_template=quote_email_template,
        invoice_email_template=invoice_email_template,
    )


@bp.route("/jobs/<jobId>/start", methods=["POST"])
def start_job(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs.jobs"))

    current_timestamp = datetime.now().strftime("%m/%d/%Y %H:%M:%S")

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {"$set": {"status": "Started", "dateStarted": current_timestamp}},
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
    if customer_id and ObjectId.is_valid(customer_id):
        customer_oid = ObjectId(customer_id)
        customer_doc = db.customers.find_one({"_id": customer_oid})
        if customer_doc:
            customer = serialize_doc(customer_doc)

    business_logo_path = resolve_current_business_logo_path(db)
    invoice_path = generate_invoice(jobId, job, customer, business_logo_path=business_logo_path)
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
    current_app.logger.info("Job completed: id=%s invoice=%s by employee_id=%s", jobId, filename, session.get("employee_id"))

    if customer_oid and customer_doc:
        current_balance = currency_to_float(customer_doc.get("balance_due", "$0.00"))
        invoice_total = currency_to_float(job.get("total", "$0.00"))
        updated_balance = normalize_currency(str(current_balance + invoice_total))

        db.customers.update_one(
            {"_id": customer_oid},
            {
                "$set": {
                    "balance_due": updated_balance,
                }
            },
        )

    return redirect(url_for("jobs.view_job", jobId=jobId))


@bp.route("/jobs/<jobId>/quote", methods=["POST"])
def create_quote(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    job_status = str(job.get("status", "")).strip().lower() if job else ""
    if not job or job_status not in {"estimate", "estimating", "pending"}:
        return redirect(url_for("jobs.jobs"))

    customer = {}
    customer_id = job.get("customer_id")
    if customer_id and ObjectId.is_valid(customer_id):
        customer_doc = db.customers.find_one({"_id": ObjectId(customer_id)})
        if customer_doc:
            customer = serialize_doc(customer_doc)

    business_logo_path = resolve_current_business_logo_path(db)
    quote_path = generate_quote(jobId, job, customer, business_logo_path=business_logo_path)
    filename = os.path.basename(quote_path)

    db.estimates.delete_many({"job_id": jobId})

    db.estimates.insert_one(
        {
            "job_id": jobId,
            "title": f"Quote for {job.get('job_type', 'Service')}",
            "date": datetime.now().strftime("%m/%d/%Y"),
            "amount": job.get("total", "$0.00"),
            "file_path": url_for("download_invoice", filename=filename),
        }
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
        entered_service_prices = request.form.getlist("service_standard_price[]") or request.form.getlist("service_price[]")
        entered_service_durations = request.form.getlist("service_estimated_hours[]") or request.form.getlist("service_duration[]")
        selected_part_names = request.form.getlist("part_code[]") or request.form.getlist("part_name[]")
        entered_part_prices = request.form.getlist("part_unit_cost[]") or request.form.getlist("part_price[]")
        selected_labor_descriptions = request.form.getlist("labor_description[]")
        entered_labor_hours = request.form.getlist("labor_hours[]")
        entered_labor_rates = request.form.getlist("labor_hourly_rate[]")
        selected_material_names = request.form.getlist("material_name[]")
        entered_material_quantities = request.form.getlist("material_quantity_used[]")
        entered_material_units = request.form.getlist("material_unit_of_measure[]")
        entered_material_prices = request.form.getlist("material_price[]")
        selected_discount_names = request.form.getlist("discount_name[]")
        entered_discount_percentages = request.form.getlist("discount_percentage[]")
        entered_discount_amounts = request.form.getlist("discount_amount[]")
        service_query = {"business_id": business_id} if business_id else {"_id": None}
        part_query = {"business_id": business_id} if business_id else {"_id": None}
        labor_query = {"business_id": business_id} if business_id else {"_id": None}
        material_query = {"business_id": business_id} if business_id else {"_id": None}
        discount_query = {"business_id": business_id} if business_id else {"_id": None}
        service_docs = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
        part_docs = [serialize_doc(part) for part in db.parts.find(part_query).sort("part_name", 1)]
        labor_docs = [serialize_doc(labor) for labor in db.labors.find(labor_query).sort("labor_description", 1)]
        material_docs = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
        discount_docs = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
        service_catalog = build_service_catalog(service_docs)
        part_catalog = build_part_catalog(part_docs)
        labor_catalog = build_labor_catalog(labor_docs)
        material_catalog = build_material_catalog(material_docs)
        discount_catalog = build_discount_catalog(discount_docs)
        services, services_total = build_job_services_from_form(
            selected_service_types,
            entered_service_prices,
            entered_service_durations,
            service_catalog,
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
        discounts, discounts_total = build_job_discounts_from_form(
            selected_discount_names,
            entered_discount_percentages,
            entered_discount_amounts,
            discount_catalog,
        )
        total = services_total + parts_total + labor_total + materials_total - discounts_total

        primary_service = services[0]["type"] if services else "No services added."
        scheduled_date = format_date(request.form.get("job_date", ""))
        scheduled_time = request.form.get("job_time", "").strip()
        job_status = resolve_job_status(
            scheduled_date,
            scheduled_time,
            services,
            parts,
            labors,
            materials,
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
            "discounts": discounts,
            "status": job_status,
            "scheduled_date": scheduled_date,
            "scheduled_time": scheduled_time,
            "address_line_1": request.form.get("job_address_line_1", "").strip(),
            "address_line_2": request.form.get("job_address_line_2", "").strip(),
            "city": request.form.get("job_city", "").strip(),
            "state": request.form.get("job_state", "").strip().upper(),
            "assigned_employee": assigned_employee,
            "total": f"${total:.2f}" if total else "$0.00",
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
    if customer_id and ObjectId.is_valid(customer_id):
        customer_doc = db.customers.find_one({"_id": ObjectId(customer_id)})
        if customer_doc:
            customer = serialize_doc(customer_doc)

    business_id = resolve_current_business_id(db)
    service_query = {"business_id": business_id} if business_id else {"_id": None}
    part_query = {"business_id": business_id} if business_id else {"_id": None}
    labor_query = {"business_id": business_id} if business_id else {"_id": None}
    material_query = {"business_id": business_id} if business_id else {"_id": None}
    discount_query = {"business_id": business_id} if business_id else {"_id": None}
    services = [serialize_doc(service) for service in db.services.find(service_query).sort("service_name", 1)]
    parts = [serialize_doc(part) for part in db.parts.find(part_query).sort("part_name", 1)]
    labors = [serialize_doc(labor) for labor in db.labors.find(labor_query).sort("labor_description", 1)]
    materials = [serialize_doc(material) for material in db.materials.find(material_query).sort("material_name", 1)]
    discounts = [serialize_doc(discount) for discount in db.discounts.find(discount_query).sort("discount_name", 1)]
    employee_options = build_employee_options(db)
    services_catalog = build_service_catalog(services)
    parts_catalog = build_part_catalog(parts)
    labors_catalog = build_labor_catalog(labors)
    materials_catalog = build_material_catalog(materials)
    discounts_catalog = build_discount_catalog(discounts)
    parts_by_id = {p["_id"]: p["part_code"] for p in parts}

    return render_template(
        "jobs/update_job.html",
        jobId=jobId,
        job=serialize_doc(job),
        customer=customer,
        services=services,
        parts=parts,
        labors=labors,
        materials=materials,
        discounts=discounts,
        employee_options=employee_options,
        services_catalog=services_catalog,
        parts_catalog=parts_catalog,
        labors_catalog=labors_catalog,
        materials_catalog=materials_catalog,
        discounts_catalog=discounts_catalog,
        parts_by_id=parts_by_id,
    )


@bp.route("/jobs/<jobId>/delete", methods=["POST"])
def delete_job(jobId):
    db = ensure_connection_or_500()
    db.jobs.delete_one({"_id": object_id_or_404(jobId)})
    current_app.logger.info("Job deleted: id=%s by employee_id=%s", jobId, session.get("employee_id"))
    return redirect(url_for("jobs.jobs"))
