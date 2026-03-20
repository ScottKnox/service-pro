from datetime import datetime
import os
import json

from bson import ObjectId
from flask import Flask, abort, redirect, render_template, request, send_file, url_for, jsonify
from flask_mail import Mail, Message

from invoice_generator import generate_invoice
from mongo import ensure_connection_or_500, object_id_or_404, serialize_doc

app = Flask(__name__)

# Flask-Mail Configuration
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'true').lower() == 'true'
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME',)
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER')

mail = Mail(app)


def normalize_currency(value: str) -> str:
    stripped = (value or "").replace("$", "").replace(",", "").strip()
    if not stripped:
        return "$0.00"
    try:
        return f"${float(stripped):.2f}"
    except ValueError:
        return "$0.00"


def normalize_duration(value: str) -> str:
    return (value or "").strip()


def format_date(date_str: str) -> str:
    if not date_str:
        return ""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return date_str


def collect_invoice_items(db):
    invoice_items = []
    jobs_with_invoices = db.jobs.find(
        {"invoices.0": {"$exists": True}},
        {"customer_name": 1, "scheduled_date": 1, "invoices": 1, "total": 1, "job_type": 1},
    ).sort([("scheduled_date", -1), ("_id", -1)])

    for job in jobs_with_invoices:
        customer_name = job.get("customer_name", "Unknown Customer")
        scheduled_date = job.get("scheduled_date", "")
        total = job.get("total", "$0.00")
        job_type = job.get("job_type", "Service")
        job_id = str(job.get("_id", ""))

        for invoice in reversed(job.get("invoices", [])):
            invoice_items.append(
                {
                    "invoice_number": invoice.get("invoice_number", "Invoice"),
                    "file_path": invoice.get("file_path", "#"),
                    "customer_name": customer_name,
                    "scheduled_date": scheduled_date,
                    "total": total,
                    "job_type": job_type,
                    "job_id": job_id,
                }
            )

    return invoice_items


def build_service_catalog(services):
    return {
        service["service_type"]: {
            "price": service.get("service_default_price", "$0.00"),
            "duration": service.get("service_duration", ""),
        }
        for service in services
    }


def build_part_catalog(parts):
    return {
        part["part_name"]: {
            "price": part.get("part_default_price", "$0.00"),
        }
        for part in parts
    }


def build_job_services_from_form(service_types, service_prices, service_durations, service_catalog):
    services = []
    total = 0.0

    for index, service_type in enumerate(service_types):
        catalog_entry = service_catalog.get(service_type, {})
        price = service_prices[index] if index < len(service_prices) else catalog_entry.get("price", "$0.00")
        duration = service_durations[index] if index < len(service_durations) else catalog_entry.get("duration", "")
        normalized_price = normalize_currency(price)
        normalized_duration = normalize_duration(duration)

        services.append(
            {
                "type": service_type,
                "price": normalized_price,
                "duration": normalized_duration,
            }
        )
        total += float(normalized_price.replace("$", "").replace(",", ""))

    return services, total


def build_job_parts_from_form(part_names, part_prices, part_catalog):
    parts = []
    total = 0.0

    for index, part_name in enumerate(part_names):
        catalog_entry = part_catalog.get(part_name, {})
        price = part_prices[index] if index < len(part_prices) else catalog_entry.get("price", "$0.00")
        normalized_price = normalize_currency(price)

        parts.append(
            {
                "name": part_name,
                "price": normalized_price,
            }
        )
        total += float(normalized_price.replace("$", "").replace(",", ""))

    return parts, total


@app.route("/")
def home():
    db = ensure_connection_or_500()
    jobs_list = [
        serialize_doc(job)
        for job in db.jobs.find().sort([("scheduled_date", 1), ("date_created", -1)])
    ]

    invoice_page_raw = request.args.get("invoice_page", "1")
    try:
        invoice_page = max(1, int(invoice_page_raw))
    except ValueError:
        invoice_page = 1

    invoices_per_page = 5
    invoice_items = collect_invoice_items(db)

    invoices_total_pages = (len(invoice_items) + invoices_per_page - 1) // invoices_per_page
    if invoices_total_pages == 0:
        invoice_page = 1
        invoices = []
    else:
        if invoice_page > invoices_total_pages:
            invoice_page = invoices_total_pages
        invoice_start = (invoice_page - 1) * invoices_per_page
        invoice_end = invoice_start + invoices_per_page
        invoices = invoice_items[invoice_start:invoice_end]

    estimate_page_raw = request.args.get("estimate_page", "1")
    try:
        estimate_page = max(1, int(estimate_page_raw))
    except ValueError:
        estimate_page = 1

    estimates_per_page = 5
    estimate_items = [
        serialize_doc(job)
        for job in db.jobs.find({"status": "Estimate"}).sort([("scheduled_date", -1), ("_id", -1)])
    ]
    estimates_total_pages = (len(estimate_items) + estimates_per_page - 1) // estimates_per_page
    if estimates_total_pages == 0:
        estimate_page = 1
        estimates = []
    else:
        if estimate_page > estimates_total_pages:
            estimate_page = estimates_total_pages
        estimate_start = (estimate_page - 1) * estimates_per_page
        estimate_end = estimate_start + estimates_per_page
        estimates = estimate_items[estimate_start:estimate_end]

    return render_template(
        "index.html",
        jobs=jobs_list,
        invoices=invoices,
        invoice_page=invoice_page,
        invoices_total_pages=invoices_total_pages,
        payments=[],
        payments_total_pages=0,
        estimates=estimates,
        estimate_page=estimate_page,
        estimates_total_pages=estimates_total_pages,
    )


@app.route("/customers")
def customers():
    db = ensure_connection_or_500()
    customers_list = [
        serialize_doc(customer)
        for customer in db.customers.find().sort([("last_name", 1), ("first_name", 1)])
    ]
    return render_template("pages/customers.html", customers=customers_list)


@app.route("/customers/add", methods=["GET", "POST"])
def add_customer():
    db = ensure_connection_or_500()
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()

        if first_name and last_name:
            customer_count = db.customers.count_documents({}) + 1
            customer = {
                "first_name": first_name,
                "last_name": last_name,
                "company": request.form.get("company", "").strip(),
                "phone": request.form.get("phone", "").strip(),
                "email": request.form.get("email", "").strip(),
                "address_line_1": request.form.get("address_line_1", "").strip(),
                "address_line_2": request.form.get("address_line_2", "").strip(),
                "city": request.form.get("city", "").strip(),
                "state": request.form.get("state", "").strip().upper(),
                "referral_source": request.form.get("referral_source", "").strip(),
                "customer_status": request.form.get("customer_status", "").strip() or "active",
                "date_added": datetime.now().strftime("%m/%d/%Y"),
                "account_number": f"ACC-{customer_count:05d}",
                "account_type": "Residential",
                "balance_due": "$0.00",
                "account_status": "Current",
            }
            inserted = db.customers.insert_one(customer)
            return redirect(url_for("view_customer", customerId=str(inserted.inserted_id)))

    return render_template("pages/add_customer.html")


@app.route("/customers/<customerId>/update", methods=["GET", "POST"])
def update_customer(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers"))

    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()

        if not first_name or not last_name:
            return render_template(
                "pages/update_customer.html",
                customerId=customerId,
                customer=serialize_doc(customer),
                error="First name and last name are required.",
            )

        update_data = {
            "first_name": first_name,
            "last_name": last_name,
            "company": request.form.get("company", "").strip(),
            "phone": request.form.get("phone", "").strip(),
            "email": request.form.get("email", "").strip(),
            "address_line_1": request.form.get("address_line_1", "").strip(),
            "address_line_2": request.form.get("address_line_2", "").strip(),
            "city": request.form.get("city", "").strip(),
            "state": request.form.get("state", "").strip().upper(),
            "referral_source": request.form.get("referral_source", "").strip(),
            "customer_status": request.form.get("customer_status", "").strip() or "active",
        }

        db.customers.update_one({"_id": ObjectId(customerId)}, {"$set": update_data})
        return redirect(url_for("view_customer", customerId=customerId))

    return render_template(
        "pages/update_customer.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        error="",
    )


@app.route("/customers/<customerId>/delete", methods=["POST"])
def delete_customer(customerId):
    db = ensure_connection_or_500()
    customer_oid = object_id_or_404(customerId)
    customer = db.customers.find_one({"_id": customer_oid})
    if not customer:
        return redirect(url_for("customers"))

    related_jobs = list(db.jobs.find({"customer_id": customerId}, {"_id": 1}))
    related_job_ids = [str(job.get("_id")) for job in related_jobs]

    db.customers.delete_one({"_id": customer_oid})
    db.jobs.delete_many({"customer_id": customerId})
    if related_job_ids:
        db.estimates.delete_many({"job_id": {"$in": related_job_ids}})

    return redirect(url_for("customers"))


@app.route("/jobs")
def jobs():
    db = ensure_connection_or_500()
    jobs_list = [
        serialize_doc(job)
        for job in db.jobs.find().sort([("scheduled_date", 1), ("date_created", -1)])
    ]
    return render_template("pages/jobs.html", jobs=jobs_list)


@app.route("/admin")
def admin():
    return render_template("pages/admin.html")


@app.route("/invoices")
def invoices():
    db = ensure_connection_or_500()
    invoice_items = collect_invoice_items(db)
    return render_template("pages/invoices.html", invoices=invoice_items)


@app.route("/services")
def manage_services():
    db = ensure_connection_or_500()
    services = [serialize_doc(service) for service in db.services.find().sort("service_type", 1)]
    return render_template(
        "pages/manage_services.html",
        services=services,
    )


@app.route("/parts")
def manage_parts():
    db = ensure_connection_or_500()
    parts = [serialize_doc(part) for part in db.parts.find().sort("part_name", 1)]
    return render_template(
        "pages/manage_parts.html",
        parts=parts,
    )


@app.route("/parts/create", methods=["GET", "POST"])
def create_part():
    db = ensure_connection_or_500()
    if request.method == "POST":
        part_name = request.form.get("part_name", "").strip()
        part_default_price = normalize_currency(request.form.get("part_price", ""))

        if part_name:
            db.parts.insert_one(
                {
                    "part_name": part_name,
                    "part_default_price": part_default_price,
                }
            )

        return redirect(url_for("manage_parts"))

    return render_template("pages/create_part.html")


@app.route("/parts/<partId>")
def view_part(partId):
    db = ensure_connection_or_500()
    part = db.parts.find_one({"_id": object_id_or_404(partId)})
    if not part:
        return redirect(url_for("manage_parts"))

    return render_template(
        "pages/view_part.html",
        partId=partId,
        part=serialize_doc(part),
    )


@app.route("/services/create", methods=["GET", "POST"])
def create_service():
    db = ensure_connection_or_500()
    if request.method == "POST":
        service_name = request.form.get("job_type", "").strip()
        service_default_price = normalize_currency(request.form.get("job_price", ""))
        service_duration = normalize_duration(request.form.get("service_duration", ""))

        if service_name:
            db.services.insert_one(
                {
                    "service_type": service_name,
                    "service_default_price": service_default_price,
                    "service_duration": service_duration,
                }
            )

        return redirect(url_for("manage_services"))

    return render_template("pages/create_service.html")


@app.route("/services/<serviceId>")
def view_service(serviceId):
    db = ensure_connection_or_500()
    service = db.services.find_one({"_id": object_id_or_404(serviceId)})
    if not service:
        return redirect(url_for("manage_services"))

    return render_template(
        "pages/view_service.html",
        serviceId=serviceId,
        service=serialize_doc(service),
    )


@app.route("/services/<serviceId>/delete", methods=["POST"])
def delete_service(serviceId):
    db = ensure_connection_or_500()
    db.services.delete_one({"_id": object_id_or_404(serviceId)})
    return redirect(url_for("manage_services"))


@app.route("/customers/<customerId>")
def view_customer(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers"))

    customer_pages = {
        "estimates": 1,
        "jobs": 1,
        "payments": 1,
    }
    customer_jobs = [
        serialize_doc(job)
        for job in db.jobs.find({"customer_id": customerId}).sort("scheduled_date", -1).limit(5)
    ]

    return render_template(
        "pages/view_customer.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        customer_pages=customer_pages,
        customer_jobs=customer_jobs,
    )


@app.route("/customers/<customerId>/jobs/create", methods=["GET", "POST"])
def create_job(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers"))

    if request.method == "POST":
        selected_service_types = [s.strip() for s in request.form.getlist("service_type[]") if s.strip()]
        entered_service_prices = [normalize_currency(p) for p in request.form.getlist("service_price[]") if p.strip()]
        entered_service_durations = [normalize_duration(d) for d in request.form.getlist("service_duration[]") if d.strip()]
        selected_part_names = [p.strip() for p in request.form.getlist("part_name[]") if p.strip()]
        entered_part_prices = [normalize_currency(p) for p in request.form.getlist("part_price[]") if p.strip()]
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
        
        primary_service = selected_service_types[0] if selected_service_types else "General Service"
        is_estimate = request.form.get("job_is_estimate", "no").strip().lower() == "yes"
        job_status = "Estimate" if is_estimate else "Scheduled"

        assigned_employee = request.form.get("job_assigned_employee", "").replace("_", " ").title()
        new_job = {
            "customer_id": customerId,
            "customer_name": f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip(),
            "company": customer.get("company", ""),
            "job_type": primary_service,
            "services": services,
            "parts": parts,
            "status": job_status,
            "scheduled_date": format_date(request.form.get("job_date", "")),
            "address_line_1": request.form.get("job_address_line_1", "").strip(),
            "address_line_2": request.form.get("job_address_line_2", "").strip(),
            "city": request.form.get("job_city", "").strip(),
            "state": request.form.get("job_state", "").strip().upper(),
            "assigned_employee": assigned_employee,
            "total": f"${total:.2f}" if total else "$0.00",
            "notes": request.form.get("job_notes", "").strip(),
            "date_created": datetime.now().strftime("%m/%d/%Y"),
            "invoices": [],
        }
        inserted = db.jobs.insert_one(new_job)
        return redirect(url_for("view_job", jobId=str(inserted.inserted_id)))

    services = [serialize_doc(service) for service in db.services.find().sort("service_type", 1)]
    parts = [serialize_doc(part) for part in db.parts.find().sort("part_name", 1)]
    services_catalog_json = json.dumps(build_service_catalog(services))
    parts_catalog_json = json.dumps(build_part_catalog(parts))

    return render_template(
        "pages/create_job.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        services=services,
        parts=parts,
        services_catalog_json=services_catalog_json,
        parts_catalog_json=parts_catalog_json,
    )


@app.route("/jobs/<jobId>")
def view_job(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs"))

    customer = {}
    customer_id = job.get("customer_id")
    if customer_id and ObjectId.is_valid(customer_id):
        customer_doc = db.customers.find_one({"_id": ObjectId(customer_id)})
        if customer_doc:
            customer = serialize_doc(customer_doc)

    # Fetch estimates for this job
    estimates = []
    estimates_list = db.estimates.find({"job_id": jobId}).sort([("date", -1)])
    estimates = [serialize_doc(estimate) for estimate in estimates_list]

    return render_template(
        "pages/view_job.html",
        jobId=jobId,
        job=serialize_doc(job),
        customer=customer,
        estimates=estimates,
    )


@app.route("/jobs/<jobId>/start", methods=["POST"])
def start_job(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs"))

    db.jobs.update_one(
        {"_id": ObjectId(jobId)},
        {"$set": {"status": "Started"}},
    )

    return redirect(url_for("view_job", jobId=jobId))


@app.route("/jobs/<jobId>/complete", methods=["POST"])
def complete_job(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs"))

    customer = {}
    customer_id = job.get("customer_id")
    if customer_id and ObjectId.is_valid(customer_id):
        customer_doc = db.customers.find_one({"_id": ObjectId(customer_id)})
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

    return redirect(url_for("view_job", jobId=jobId))


@app.route("/jobs/<jobId>/quote", methods=["POST"])
def create_quote(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job or job.get("status") != "Estimate":
        return redirect(url_for("jobs"))

    customer = {}
    customer_id = job.get("customer_id")
    if customer_id and ObjectId.is_valid(customer_id):
        customer_doc = db.customers.find_one({"_id": ObjectId(customer_id)})
        if customer_doc:
            customer = serialize_doc(customer_doc)

    from invoice_generator import generate_quote
    quote_path = generate_quote(jobId, job, customer)
    filename = os.path.basename(quote_path)

    db.estimates.insert_one({
        "job_id": jobId,
        "title": f"Quote for {job.get('job_type', 'Service')}",
        "date": datetime.now().strftime("%m/%d/%Y"),
        "amount": job.get("total", "$0.00"),
        "file_path": url_for("download_invoice", filename=filename),
    })

    return redirect(url_for("view_job", jobId=jobId))


@app.route("/jobs/<jobId>/email-estimate", methods=["POST"])
def send_estimate_email(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404

    customer = {}
    customer_id = job.get("customer_id")
    if customer_id and ObjectId.is_valid(customer_id):
        customer_doc = db.customers.find_one({"_id": ObjectId(customer_id)})
        if customer_doc:
            customer = serialize_doc(customer_doc)

    try:
        data = request.get_json()
        recipient_email = data.get('recipient_email', '')
        subject = data.get('subject', '')
        body = data.get('body', '')
        estimate_file = data.get('estimate_file', '')

        if not recipient_email or not subject or not body:
            return jsonify({"success": False, "error": "Missing required fields"}), 400

        # Extract filename from the estimate file path
        filename = estimate_file.split('/')[-1]
        invoices_dir = os.path.join(os.path.dirname(__file__), "invoices")
        filepath = os.path.join(invoices_dir, filename)

        # Verify file exists
        if not os.path.exists(filepath) or not os.path.abspath(filepath).startswith(os.path.abspath(invoices_dir)):
            return jsonify({"success": False, "error": "Estimate file not found"}), 404

        # Create email message
        msg = Message(
            subject=subject,
            recipients=[recipient_email],
            body=body
        )

        # Attach PDF file
        with open(filepath, 'rb') as f:
            msg.attach(filename, 'application/pdf', f.read())

        # Send email
        mail.send(msg)

        return jsonify({"success": True}), 200

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/jobs/<jobId>/update", methods=["GET", "POST"])
def update_job(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs"))

    if request.method == "POST":
        selected_service_types = [s.strip() for s in request.form.getlist("service_type[]") if s.strip()]
        entered_service_prices = [normalize_currency(p) for p in request.form.getlist("service_price[]") if p.strip()]
        entered_service_durations = [normalize_duration(d) for d in request.form.getlist("service_duration[]") if d.strip()]
        selected_part_names = [p.strip() for p in request.form.getlist("part_name[]") if p.strip()]
        entered_part_prices = [normalize_currency(p) for p in request.form.getlist("part_price[]") if p.strip()]
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
        
        primary_service = selected_service_types[0] if selected_service_types else job.get("job_type", "General Service")
        is_estimate = request.form.get("job_is_estimate", "no").strip().lower() == "yes"
        job_status = "Estimate" if is_estimate else "Scheduled"

        assigned_employee = request.form.get("job_assigned_employee", "").replace("_", " ").title()
        
        update_data = {
            "job_type": primary_service,
            "services": services,
            "parts": parts,
            "status": job_status,
            "scheduled_date": format_date(request.form.get("job_date", "")),
            "address_line_1": request.form.get("job_address_line_1", "").strip(),
            "address_line_2": request.form.get("job_address_line_2", "").strip(),
            "city": request.form.get("job_city", "").strip(),
            "state": request.form.get("job_state", "").strip().upper(),
            "assigned_employee": assigned_employee,
            "total": f"${total:.2f}" if total else "$0.00",
            "notes": request.form.get("job_notes", "").strip(),
        }
        
        db.jobs.update_one(
            {"_id": ObjectId(jobId)},
            {"$set": update_data}
        )
        
        return redirect(url_for("view_job", jobId=jobId))

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
        "pages/update_job.html",
        jobId=jobId,
        job=serialize_doc(job),
        customer=customer,
        services=services,
        parts=parts,
        services_catalog_json=services_catalog_json,
        parts_catalog_json=parts_catalog_json,
    )


@app.route("/jobs/<jobId>/delete", methods=["POST"])
def delete_job(jobId):
    db = ensure_connection_or_500()
    db.jobs.delete_one({"_id": object_id_or_404(jobId)})
    return redirect(url_for("jobs"))

@app.route("/invoices/<filename>")
def download_invoice(filename):
    """Serve invoice PDFs from the invoices directory."""
    invoices_dir = os.path.join(os.path.dirname(__file__), "invoices")
    filepath = os.path.join(invoices_dir, filename)
    
    # Verify the file exists and is in the invoices directory (security check)
    if os.path.exists(filepath) and os.path.abspath(filepath).startswith(os.path.abspath(invoices_dir)):
        return send_file(filepath, mimetype="application/pdf", as_attachment=False)
    else:
        return "Invoice not found", 404


if __name__ == "__main__":
    app.run(debug=True)
