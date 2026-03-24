from datetime import datetime
import logging
import os
import json

from bson import ObjectId
from flask import Flask, abort, redirect, render_template, request, send_file, url_for, jsonify, session
from flask_mail import Mail, Message
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import check_password_hash, generate_password_hash

from invoice_generator import generate_invoice
from mongo import ensure_connection_or_500, object_id_or_404, serialize_doc
from utils.currency import normalize_currency, currency_to_float
from utils.formatters import format_date, normalize_duration
from utils.catalog import build_service_catalog, build_part_catalog, build_job_services_from_form, build_job_parts_from_form
from utils.invoices import collect_invoice_items

app = Flask(__name__)

# Session Configuration
_secret_key = os.getenv('SECRET_KEY')
if not _secret_key:
    raise RuntimeError("SECRET_KEY environment variable is not set")
app.secret_key = _secret_key

# Flask-Mail Configuration
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'true').lower() == 'true'
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME',)
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER')

mail = Mail(app)
csrf = CSRFProtect(app)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", error_message="The page you requested could not be found."), 404


@app.errorhandler(500)
def internal_error(e):
    logger.error("Internal server error: %s", e)
    return render_template("error.html", error_message="An internal server error occurred. Please try again later."), 500


@app.before_request
def require_login():
    """Redirect unauthenticated users to login for all protected endpoints."""
    open_endpoints = {'login', 'logout', 'static', 'home', 'error_page'}
    if request.endpoint not in open_endpoints:
        employee_id = session.get("employee_id")
        if not employee_id or not ObjectId.is_valid(employee_id):
            session.clear()
            return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    db = ensure_connection_or_500()
    error = None
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        
        if not username or not password:
            error = "Username and password are required."
        else:
            # Find employee by username
            employee = db.employees.find_one({"username": username})
            
            if not employee or not check_password_hash(employee.get("password", ""), password):
                logger.warning("Failed login attempt for username=%r from %s", username, request.remote_addr)
                error = "Invalid username or password."
            else:
                # Login successful - set session
                session["employee_id"] = str(employee["_id"])
                session["employee_name"] = f"{employee.get('first_name', '')} {employee.get('last_name', '')}".strip()
                session["employee_position"] = (employee.get("position") or "").strip()
                logger.info("Login: employee_id=%s username=%r from %s", session["employee_id"], username, request.remote_addr)
                return redirect(url_for("home"))
    
    return render_template("auth/login.html", error=error)


@app.route("/logout")
def logout():
    logger.info("Logout: employee_id=%s", session.get("employee_id"))
    session.clear()
    return redirect(url_for("login"))


@app.route("/profile")
def view_profile():
    db = ensure_connection_or_500()
    employee_id = session.get("employee_id")
    employee = db.employees.find_one({"_id": ObjectId(employee_id)})
    if not employee:
        session.clear()
        return redirect(url_for("login"))

    return render_template("profile/view_profile.html", employee=serialize_doc(employee))


@app.route("/profile/update", methods=["GET", "POST"])
def update_profile():
    db = ensure_connection_or_500()
    employee_id = session.get("employee_id")
    employee = db.employees.find_one({"_id": ObjectId(employee_id)})
    if not employee:
        session.clear()
        return redirect(url_for("login"))

    error = ""
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()

        if not first_name or not last_name:
            error = "First name and last name are required."
        else:
            update_data = {
                "first_name": first_name,
                "last_name": last_name,
                "phone": request.form.get("phone", "").strip(),
                "email": request.form.get("email", "").strip(),
                "position": request.form.get("position", "").strip(),
                "bio": request.form.get("bio", "").strip(),
                "profile_updated_at": datetime.now().strftime("%m/%d/%Y %H:%M:%S"),
            }

            db.employees.update_one({"_id": ObjectId(employee_id)}, {"$set": update_data})
            employee = db.employees.find_one({"_id": ObjectId(employee_id)})

            # Keep header/session identity in sync with profile edits.
            session["employee_name"] = f"{employee.get('first_name', '')} {employee.get('last_name', '')}".strip()
            session["employee_position"] = (employee.get("position") or "").strip()

            return redirect(url_for("view_profile"))

    return render_template("profile/update_profile.html", employee=serialize_doc(employee), error=error)


@app.route("/")
def home():
    db = ensure_connection_or_500()
    is_logged_in = bool(session.get("employee_id"))

    if not is_logged_in:
        return render_template("index.html", is_logged_in=False)

    current_employee_name = (session.get("employee_name") or "").strip()
    normalized_current_employee_name = " ".join(current_employee_name.lower().split())

    jobs_list = [
        serialize_doc(job)
        for job in db.jobs.find().sort([("scheduled_date", 1), ("date_created", -1)])
    ]
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
                "checked": normalized_full_name == normalized_current_employee_name,
            }
        )

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
        is_logged_in=True,
        jobs=jobs_list,
        employee_filters=employee_filters,
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
    return render_template("customers/customers.html", customers=customers_list)


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
            logger.info("Customer created: id=%s by employee_id=%s", str(inserted.inserted_id), session.get("employee_id"))
            return redirect(url_for("view_customer", customerId=str(inserted.inserted_id)))

    return render_template("customers/add_customer.html")


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
                "customers/update_customer.html",
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
        "customers/update_customer.html",
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
    db.equipment.delete_many({"customer_id": customerId})
    if related_job_ids:
        db.estimates.delete_many({"job_id": {"$in": related_job_ids}})
    logger.info("Customer deleted: id=%s by employee_id=%s", customerId, session.get("employee_id"))
    return redirect(url_for("customers"))


@app.route("/employees")
def employees():
    db = ensure_connection_or_500()
    employees_list = [
        serialize_doc(employee)
        for employee in db.employees.find().sort([("last_name", 1), ("first_name", 1)])
    ]
    return render_template("employees/employees.html", employees=employees_list)


@app.route("/employees/add", methods=["GET", "POST"])
def add_employee():
    db = ensure_connection_or_500()
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if first_name and last_name and username and password:
            employee_count = db.employees.count_documents({}) + 1
            employee = {
                "first_name": first_name,
                "last_name": last_name,
                "username": username,
                "password": generate_password_hash(password, method="scrypt"),
                "phone": request.form.get("phone", "").strip(),
                "email": request.form.get("email", "").strip(),
                "position": request.form.get("position", "").strip(),
                "bio": request.form.get("bio", "").strip(),
                "status": request.form.get("status", "").strip() or "active",
                "date_added": datetime.now().strftime("%m/%d/%Y"),
                "employee_id": f"EMP-{employee_count:05d}",
            }
            inserted = db.employees.insert_one(employee)
            logger.info("Employee created: id=%s username=%r by employee_id=%s", str(inserted.inserted_id), username, session.get("employee_id"))
            return redirect(url_for("view_employee", employeeId=str(inserted.inserted_id)))

    return render_template("employees/add_employee.html")


@app.route("/employees/<employeeId>")
def view_employee(employeeId):
    db = ensure_connection_or_500()
    employee = db.employees.find_one({"_id": object_id_or_404(employeeId)})
    if not employee:
        return redirect(url_for("employees"))

    return render_template(
        "employees/view_employee.html",
        employeeId=employeeId,
        employee=serialize_doc(employee),
    )


@app.route("/employees/<employeeId>/update", methods=["GET", "POST"])
def update_employee(employeeId):
    db = ensure_connection_or_500()
    employee = db.employees.find_one({"_id": object_id_or_404(employeeId)})
    if not employee:
        return redirect(url_for("employees"))

    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not first_name or not last_name or not username:
            return render_template(
                "employees/update_employee.html",
                employeeId=employeeId,
                employee=serialize_doc(employee),
                error="First name, last name, and username are required.",
            )

        update_data = {
            "first_name": first_name,
            "last_name": last_name,
            "username": username,
            "phone": request.form.get("phone", "").strip(),
            "email": request.form.get("email", "").strip(),
            "position": request.form.get("position", "").strip(),
            "bio": request.form.get("bio", "").strip(),
            "status": request.form.get("status", "").strip() or "active",
        }
        if password:
            update_data["password"] = generate_password_hash(password, method="scrypt")

        db.employees.update_one({"_id": ObjectId(employeeId)}, {"$set": update_data})
        return redirect(url_for("view_employee", employeeId=employeeId))

    return render_template(
        "employees/update_employee.html",
        employeeId=employeeId,
        employee=serialize_doc(employee),
        error="",
    )


@app.route("/employees/<employeeId>/delete", methods=["POST"])
def delete_employee(employeeId):
    db = ensure_connection_or_500()
    employee_oid = object_id_or_404(employeeId)
    employee = db.employees.find_one({"_id": employee_oid})
    if not employee:
        return redirect(url_for("employees"))

    db.employees.delete_one({"_id": employee_oid})
    logger.info("Employee deleted: id=%s by employee_id=%s", employeeId, session.get("employee_id"))
    return redirect(url_for("employees"))


@app.route("/jobs")
def jobs():
    db = ensure_connection_or_500()
    jobs_list = [
        serialize_doc(job)
        for job in db.jobs.find().sort([("scheduled_date", 1), ("date_created", -1)])
    ]
    return render_template("jobs/jobs.html", jobs=jobs_list)


@app.route("/admin")
def admin():
    return render_template("admin/admin.html")


@app.route("/invoices")
def invoices():
    db = ensure_connection_or_500()
    invoice_items = collect_invoice_items(db)
    return render_template("invoices/invoices.html", invoices=invoice_items)


@app.route("/services")
def manage_services():
    db = ensure_connection_or_500()
    services = [serialize_doc(service) for service in db.services.find().sort("service_type", 1)]
    return render_template(
        "services/manage_services.html",
        services=services,
    )


@app.route("/parts")
def manage_parts():
    db = ensure_connection_or_500()
    parts = [serialize_doc(part) for part in db.parts.find().sort("part_name", 1)]
    return render_template(
        "services/manage_parts.html",
        parts=parts,
    )


@app.route("/business")
def business_profile():
    db = ensure_connection_or_500()

    employee_id = session.get("employee_id")
    employee = db.employees.find_one({"_id": ObjectId(employee_id)})
    if not employee:
        session.clear()
        return redirect(url_for("login"))

    business_ref = employee.get("business")
    if not business_ref:
        return redirect(url_for("error_page", error="no_business"))

    business_oid = None
    if isinstance(business_ref, ObjectId):
        business_oid = business_ref
    elif isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
        business_oid = ObjectId(business_ref)

    if not business_oid:
        return redirect(url_for("error_page", error="no_business"))

    # Get the business profile for the logged-in employee's assigned business
    business = db.businesses.find_one({"_id": business_oid})

    if not business:
        return redirect(url_for("error_page", error="no_business"))

    business = serialize_doc(business)

    return render_template("business/business_profile.html", business=business)


@app.route("/business/update", methods=["GET", "POST"])
def update_business():
    db = ensure_connection_or_500()

    employee_id = session.get("employee_id")
    employee = db.employees.find_one({"_id": ObjectId(employee_id)})
    if not employee:
        session.clear()
        return redirect(url_for("login"))

    business_ref = employee.get("business")
    if not business_ref:
        return redirect(url_for("error_page", error="no_business"))

    business_oid = None
    if isinstance(business_ref, ObjectId):
        business_oid = business_ref
    elif isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
        business_oid = ObjectId(business_ref)

    if not business_oid:
        return redirect(url_for("error_page", error="no_business"))

    if request.method == "POST":
        company_name = request.form.get("company_name", "").strip()
        tax_rate = request.form.get("tax_rate", "0").strip()
        quote_email_template = request.form.get("quote_email_template", "").strip()
        invoice_email_template = request.form.get("invoice_email_template", "").strip()

        db.businesses.update_one(
            {"_id": business_oid},
            {
                "$set": {
                    "company_name": company_name,
                    "tax_rate": tax_rate,
                    "quote_email_template": quote_email_template,
                    "invoice_email_template": invoice_email_template,
                }
            },
        )

        return redirect(url_for("business_profile"))

    business = db.businesses.find_one({"_id": business_oid})
    if not business:
        return redirect(url_for("error_page", error="no_business"))

    business = serialize_doc(business)
    return render_template("business/update_business.html", business=business)


@app.route("/error")
def error_page():
    error_type = request.args.get("error", "unknown")
    error_messages = {
        "no_business": "No business onboarded for logged in employee",
        "unknown": "An error occurred"
    }
    error_message = error_messages.get(error_type, error_messages["unknown"])
    return render_template("error.html", error_message=error_message)


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

    return render_template("services/create_part.html")


@app.route("/parts/<partId>")
def view_part(partId):
    db = ensure_connection_or_500()
    part = db.parts.find_one({"_id": object_id_or_404(partId)})
    if not part:
        return redirect(url_for("manage_parts"))

    return render_template(
        "services/view_part.html",
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

    return render_template("services/create_service.html")


@app.route("/services/<serviceId>")
def view_service(serviceId):
    db = ensure_connection_or_500()
    service = db.services.find_one({"_id": object_id_or_404(serviceId)})
    if not service:
        return redirect(url_for("manage_services"))

    return render_template(
        "services/view_service.html",
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

    jobs_page_raw = request.args.get("jobs_page", "1")
    payments_page_raw = request.args.get("payments_page", "1")
    equipment_page_raw = request.args.get("equipment_page", "1")

    try:
        jobs_page = max(1, int(jobs_page_raw))
    except ValueError:
        jobs_page = 1

    try:
        payments_page = max(1, int(payments_page_raw))
    except ValueError:
        payments_page = 1

    try:
        equipment_page = max(1, int(equipment_page_raw))
    except ValueError:
        equipment_page = 1

    jobs_per_page = 5
    payments_per_page = 5
    equipment_per_page = 5

    customer_jobs_total = db.jobs.count_documents({"customer_id": customerId})
    customer_jobs_total_pages = (customer_jobs_total + jobs_per_page - 1) // jobs_per_page
    if customer_jobs_total_pages == 0:
        jobs_page = 1
    elif jobs_page > customer_jobs_total_pages:
        jobs_page = customer_jobs_total_pages

    customer_payments_total = db.payments.count_documents({"customer_id": customerId})
    customer_payments_total_pages = (customer_payments_total + payments_per_page - 1) // payments_per_page
    if customer_payments_total_pages == 0:
        payments_page = 1
    elif payments_page > customer_payments_total_pages:
        payments_page = customer_payments_total_pages

    customer_equipment_total = db.equipment.count_documents({"customer_id": customerId})
    customer_equipment_total_pages = (customer_equipment_total + equipment_per_page - 1) // equipment_per_page
    if customer_equipment_total_pages == 0:
        equipment_page = 1
    elif equipment_page > customer_equipment_total_pages:
        equipment_page = customer_equipment_total_pages

    customer_pages = {
        "jobs": jobs_page,
        "payments": payments_page,
        "equipment": equipment_page,
    }

    jobs_skip = (jobs_page - 1) * jobs_per_page
    customer_jobs = [
        serialize_doc(job)
        for job in db.jobs.find({"customer_id": customerId}).sort("scheduled_date", -1).skip(jobs_skip).limit(jobs_per_page)
    ]

    payments_skip = (payments_page - 1) * payments_per_page
    customer_payments = [
        serialize_doc(payment)
        for payment in db.payments.find({"customer_id": customerId}).sort([("date", -1), ("_id", -1)]).skip(payments_skip).limit(payments_per_page)
    ]
    equipment_skip = (equipment_page - 1) * equipment_per_page
    customer_equipment = [
        serialize_doc(equipment)
        for equipment in db.equipment.find({"customer_id": customerId}).sort([("equipment_name", 1), ("_id", -1)]).skip(equipment_skip).limit(equipment_per_page)
    ]

    return render_template(
        "customers/view_customer.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        customer_pages=customer_pages,
        customer_jobs=customer_jobs,
        customer_jobs_total_pages=customer_jobs_total_pages,
        customer_payments=customer_payments,
        customer_payments_total_pages=customer_payments_total_pages,
        customer_equipment=customer_equipment,
        customer_equipment_total_pages=customer_equipment_total_pages,
    )


@app.route("/customers/<customerId>/equipment/add", methods=["GET", "POST"])
def add_equipment(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers"))

    error = ""
    if request.method == "POST":
        equipment_name = request.form.get("equipment_name", "").strip()
        serial_number = request.form.get("serial_number", "").strip()
        brand = request.form.get("brand", "").strip()
        equipment_location = request.form.get("equipment_location", "").strip()

        if not equipment_name:
            error = "Equipment name is required."
        elif not serial_number:
            error = "Serial number is required."
        elif not brand:
            error = "Brand is required."
        else:
            equipment = {
                "customer_id": customerId,
                "equipment_name": equipment_name,
                "serial_number": serial_number,
                "brand": brand,
                "equipment_location": equipment_location,
            }
            inserted = db.equipment.insert_one(equipment)
            return redirect(url_for("view_equipment", customerId=customerId, equipmentId=str(inserted.inserted_id)))

    return render_template(
        "equipment/add_equipment.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        error=error,
    )


@app.route("/customers/<customerId>/equipment/<equipmentId>")
def view_equipment(customerId, equipmentId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers"))

    equipment = db.equipment.find_one({"_id": object_id_or_404(equipmentId), "customer_id": customerId})
    if not equipment:
        return redirect(url_for("view_customer", customerId=customerId))

    return render_template(
        "equipment/view_equipment.html",
        customerId=customerId,
        equipmentId=equipmentId,
        customer=serialize_doc(customer),
        equipment=serialize_doc(equipment),
    )


@app.route("/customers/<customerId>/equipment/<equipmentId>/update", methods=["GET", "POST"])
def update_equipment(customerId, equipmentId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers"))

    equipment = db.equipment.find_one({"_id": object_id_or_404(equipmentId), "customer_id": customerId})
    if not equipment:
        return redirect(url_for("view_customer", customerId=customerId))

    error = ""
    if request.method == "POST":
        equipment_name = request.form.get("equipment_name", "").strip()
        serial_number = request.form.get("serial_number", "").strip()
        brand = request.form.get("brand", "").strip()
        equipment_location = request.form.get("equipment_location", "").strip()

        if not equipment_name:
            error = "Equipment name is required."
        elif not serial_number:
            error = "Serial number is required."
        elif not brand:
            error = "Brand is required."
        else:
            update_data = {
                "equipment_name": equipment_name,
                "serial_number": serial_number,
                "brand": brand,
                "equipment_location": equipment_location,
            }

            db.equipment.update_one({"_id": ObjectId(equipmentId), "customer_id": customerId}, {"$set": update_data})
            return redirect(url_for("view_equipment", customerId=customerId, equipmentId=equipmentId))

    return render_template(
        "equipment/update_equipment.html",
        customerId=customerId,
        equipmentId=equipmentId,
        customer=serialize_doc(customer),
        equipment=serialize_doc(equipment),
        error=error,
    )


@app.route("/customers/<customerId>/equipment/<equipmentId>/delete", methods=["POST"])
def delete_equipment(customerId, equipmentId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers"))

    db.equipment.delete_one({"_id": object_id_or_404(equipmentId), "customer_id": customerId})
    return redirect(url_for("view_customer", customerId=customerId))


@app.route("/customers/<customerId>/jobs/create", methods=["GET", "POST"])
def create_job(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers"))

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
        
        primary_service = services[0]["type"] if services else "General Service"
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
        logger.info("Job created: id=%s customer_id=%s by employee_id=%s", str(inserted.inserted_id), customerId, session.get("employee_id"))
        return redirect(url_for("view_job", jobId=str(inserted.inserted_id)))

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


@app.route("/jobs/<jobId>")
def view_job(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs"))

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

    # Fetch estimates for this job
    estimates = []
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
    logger.info("Job completed: id=%s invoice=%s by employee_id=%s", jobId, filename, session.get("employee_id"))

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
        logger.info("Estimate email sent: job_id=%s to=%r by employee_id=%s", jobId, recipient_email, session.get("employee_id"))
        return jsonify({"success": True}), 200

    except Exception as e:
        logger.error("Email send failed: job_id=%s error=%s", jobId, e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/jobs/<jobId>/update", methods=["GET", "POST"])
def update_job(jobId):
    db = ensure_connection_or_500()
    job = db.jobs.find_one({"_id": object_id_or_404(jobId)})
    if not job:
        return redirect(url_for("jobs"))

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
        
        primary_service = services[0]["type"] if services else job.get("job_type", "General Service")
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
        "jobs/update_job.html",
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
    logger.info("Job deleted: id=%s by employee_id=%s", jobId, session.get("employee_id"))
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
