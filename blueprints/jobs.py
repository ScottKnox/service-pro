from datetime import datetime
import json
import os

from bson import ObjectId
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for
from flask_mail import Message

from invoice_generator import generate_invoice, generate_quote
from mongo import ensure_connection_or_500, object_id_or_404, serialize_doc
from utils.catalog import build_part_catalog, build_service_catalog, build_job_parts_from_form, build_job_services_from_form
from utils.currency import currency_to_float, normalize_currency
from utils.formatters import format_date

bp = Blueprint("jobs", __name__)


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
        selected_service_types = request.form.getlist("service_type[]")
        entered_service_prices = request.form.getlist("service_price[]")
        entered_service_durations = request.form.getlist("service_duration[]")
        selected_part_names = request.form.getlist("part_name[]")
        entered_part_prices = request.form.getlist("part_price[]")
        service_docs = [serialize_doc(service) for service in db.services.find().sort("service_type", 1)]
        part_docs = [serialize_doc(part) for part in db.parts.find().sort("part_name", 1)]
        service_catalog = build_service_catalog(service_docs)
        part_catalog = build_part_catalog(part_docs)
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
        total = services_total + parts_total

        primary_service = services[0]["type"] if services else "No services added."
        scheduled_date = format_date(request.form.get("job_date", ""))
        scheduled_time = request.form.get("job_time", "").strip()
        job_status = "Scheduled" if scheduled_date and scheduled_time else "Pending"

        assigned_employee = request.form.get("job_assigned_employee", "").replace("_", " ").title()
        
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

    services = [serialize_doc(service) for service in db.services.find().sort("service_type", 1)]
    parts = [serialize_doc(part) for part in db.parts.find().sort("part_name", 1)]
    services_catalog_json = json.dumps(build_service_catalog(services))
    parts_catalog_json = json.dumps(build_part_catalog(parts))

    return render_template(
        "jobs/create_job.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        services=services,
        parts=parts,
        services_catalog_json=services_catalog_json,
        parts_catalog_json=parts_catalog_json,
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

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {"$set": {"status": "Started"}},
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

    invoice_path = generate_invoice(jobId, job, customer)
    filename = os.path.basename(invoice_path)

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {
            "$set": {"status": "Completed"},
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

    quote_path = generate_quote(jobId, job, customer)
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
        selected_service_types = request.form.getlist("service_type[]")
        entered_service_prices = request.form.getlist("service_price[]")
        entered_service_durations = request.form.getlist("service_duration[]")
        selected_part_names = request.form.getlist("part_name[]")
        entered_part_prices = request.form.getlist("part_price[]")
        service_docs = [serialize_doc(service) for service in db.services.find().sort("service_type", 1)]
        part_docs = [serialize_doc(part) for part in db.parts.find().sort("part_name", 1)]
        service_catalog = build_service_catalog(service_docs)
        part_catalog = build_part_catalog(part_docs)
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
        total = services_total + parts_total

        primary_service = services[0]["type"] if services else "No services added."
        existing_services = job.get("services", [])
        existing_parts = job.get("parts", [])
        added_services = len(services) > len(existing_services)
        added_parts = len(parts) > len(existing_parts)
        job_status = job.get("status", "Scheduled")
        if added_services or added_parts:
            job_status = "Estimating"

        assigned_employee = request.form.get("job_assigned_employee", "").replace("_", " ").title()

        # Prepare notes update - add new note to collection if provided
        update_data = {
            "job_type": primary_service,
            "services": services,
            "parts": parts,
            "status": job_status,
            "scheduled_date": format_date(request.form.get("job_date", "")),
            "scheduled_time": request.form.get("job_time", "").strip(),
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

    services = [serialize_doc(service) for service in db.services.find().sort("service_type", 1)]
    parts = [serialize_doc(part) for part in db.parts.find().sort("part_name", 1)]
    services_catalog_json = json.dumps(build_service_catalog(services))
    parts_catalog_json = json.dumps(build_part_catalog(parts))

    return render_template(
        "jobs/update_job.html",
        jobId=jobId,
        job=serialize_doc(job),
        customer=customer,
        services=services,
        parts=parts,
        services_catalog_json=services_catalog_json,
        parts_catalog_json=parts_catalog_json,
    )


@bp.route("/jobs/<jobId>/delete", methods=["POST"])
def delete_job(jobId):
    db = ensure_connection_or_500()
    db.jobs.delete_one({"_id": object_id_or_404(jobId)})
    current_app.logger.info("Job deleted: id=%s by employee_id=%s", jobId, session.get("employee_id"))
    return redirect(url_for("jobs.jobs"))
