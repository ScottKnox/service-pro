from datetime import datetime
import logging
import os

from bson import ObjectId
from flask import Flask, redirect, render_template, request, send_file, session, url_for
from flask_mail import Mail
from flask_wtf.csrf import CSRFProtect

from blueprints import register_blueprints
from mongo import ensure_connection_or_500, serialize_doc
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

    jobs_list = [
        serialize_doc(job)
        for job in db.jobs.find().sort([("scheduled_date", 1), ("scheduled_time", 1), ("date_created", -1)])
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
                "checked": current_employee_position == "clerk" or normalized_full_name == normalized_current_employee_name,
            }
        )

    pending_page_raw = request.args.get("pending_page", "1")
    try:
        pending_page = max(1, int(pending_page_raw))
    except ValueError:
        pending_page = 1

    pending_jobs_per_page = 5
    pending_jobs_all = []
    for job in db.jobs.find({"status": {"$regex": "^Pending$", "$options": "i"}}).sort([("date_created", -1), ("_id", -1)]):
        serialized_job = serialize_doc(job)
        customer_phone = "N/A"
        customer_id = serialized_job.get("customer_id")
        if customer_id and ObjectId.is_valid(customer_id):
            customer_doc = db.customers.find_one({"_id": ObjectId(customer_id)}, {"phone": 1})
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

    estimate_page_raw = request.args.get("estimate_page", "1")
    try:
        estimate_page = max(1, int(estimate_page_raw))
    except ValueError:
        estimate_page = 1

    estimates_per_page = 5
    estimating_jobs = [
        serialize_doc(job)
        for job in db.jobs.find({"status": {"$regex": "^Estimating$", "$options": "i"}}).sort([("scheduled_date", -1), ("scheduled_time", -1), ("_id", -1)])
    ]

    job_ids = [job.get("_id") for job in estimating_jobs if job.get("_id")]
    estimate_docs = [
        serialize_doc(estimate)
        for estimate in db.estimates.find({"job_id": {"$in": job_ids}}).sort([("date", -1), ("_id", -1)])
    ] if job_ids else []

    latest_estimate_by_job = {}
    for estimate_doc in estimate_docs:
        estimate_job_id = estimate_doc.get("job_id")
        if estimate_job_id and estimate_job_id not in latest_estimate_by_job:
            latest_estimate_by_job[estimate_job_id] = estimate_doc

    estimate_items = []
    for job in estimating_jobs:
        job_id = job.get("_id", "")
        estimate_doc = latest_estimate_by_job.get(job_id)
        has_quote_file = bool(estimate_doc and estimate_doc.get("file_path"))
        assigned_employee = (job.get("assigned_employee") or "").strip()

        estimate_items.append(
            {
                "date": (estimate_doc or {}).get("date") or job.get("date_created") or "",
                "customer_name": job.get("customer_name") or "Unknown Customer",
                "title": (estimate_doc or {}).get("title") or job.get("job_type") or "Estimate",
                "amount": (estimate_doc or {}).get("amount") or job.get("total") or "$0.00",
                "href": (estimate_doc or {}).get("file_path") if has_quote_file else url_for("jobs.view_job", jobId=job_id),
                "open_in_new_tab": has_quote_file,
                "assigned_employee": assigned_employee,
                "assigned_employee_key": assigned_employee.lower().replace(" ", "-"),
            }
        )

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
        pending_jobs=pending_jobs,
        pending_page=pending_page,
        pending_total_pages=pending_total_pages,
        payments=[],
        payments_total_pages=0,
        estimates=estimates,
        estimate_page=estimate_page,
        estimates_total_pages=estimates_total_pages,
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
