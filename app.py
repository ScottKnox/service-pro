from flask import Flask, redirect, render_template, request, url_for, send_file
from invoice_generator import generate_invoice
import os

app = Flask(__name__)

MOCKED_CUSTOMERS = {
    1: {
        "first_name": "Emma",
        "last_name": "Collins",
        "company": "Collins Heating & Cooling",
        "phone": "(615) 555-0183",
        "email": "emma.collins@email.com",
        "address_line_1": "1234 Magnolia Ave",
        "address_line_2": "Apt 5B",
        "city": "Nashville",
        "state": "TN",
        "referral_source": "Google",
        "customer_status": "Active",
        "date_added": "02/14/2026",
        "account_number": "ACC-00142",
        "account_type": "Residential",
        "balance_due": "$499.99",
        "account_status": "Current",
    },
    2: {
        "first_name": "Austin",
        "last_name": "Sanders",
        "company": "",
        "phone": "(615) 555-0135",
        "email": "austin.sanders@email.com",
        "address_line_1": "456 Oak Street",
        "address_line_2": "",
        "city": "Franklin",
        "state": "TN",
        "referral_source": "Past Customer",
        "customer_status": "Active",
        "date_added": "03/01/2026",
        "account_number": "ACC-00158",
        "account_type": "Residential",
        "balance_due": "$0.00",
        "account_status": "Current",
    },
}

DEFAULT_CUSTOMER = {
    "first_name": "Unknown",
    "last_name": "Customer",
    "company": "",
    "phone": "",
    "email": "",
    "address_line_1": "",
    "address_line_2": "",
    "city": "",
    "state": "",
    "referral_source": "",
    "customer_status": "",
    "date_added": "",
    "account_number": "",
    "account_type": "",
    "balance_due": "",
    "account_status": "",
}

MOCKED_CUSTOMER_PAGES = {
    1: {
        "estimates": 1,
        "jobs": 2,
        "payments": 1,
    },
    2: {
        "estimates": 2,
        "jobs": 1,
        "payments": 3,
    },
}

DEFAULT_CUSTOMER_PAGES = {
    "estimates": 1,
    "jobs": 1,
    "payments": 1,
}

MOCKED_JOBS = {
    1: {
        "customer_id": 1,
        "customer_name": "Emma Collins",
        "job_type": "Spring Tune-Up",
        "status": "Scheduled",
        "scheduled_date": "03/22/2026",
        "address_line_1": "1234 Magnolia Ave",
        "address_line_2": "Apt 5B",
        "city": "Nashville",
        "state": "TN",
        "assigned_employee": "Scott Knox",
        "price": "$199.99",
        "notes": "Standard spring maintenance including filter check and system diagnostics.",
        "date_created": "03/15/2026",
    },
    2: {
        "customer_id": 2,
        "customer_name": "Austin Sanders",
        "job_type": "Blower Motor Replacement",
        "status": "In Progress",
        "scheduled_date": "03/25/2026",
        "address_line_1": "456 Oak Street",
        "address_line_2": "",
        "city": "Franklin",
        "state": "TN",
        "assigned_employee": "Daniel Essary",
        "price": "$499.99",
        "notes": "Replace faulty blower motor, installation of new unit and testing.",
        "date_created": "03/18/2026",
    },
    3: {
        "customer_id": "",
        "customer_name": "Chloe Wooten",
        "job_type": "Capacitor Replacement",
        "status": "Waiting Parts",
        "scheduled_date": "03/28/2026",
        "address_line_1": "",
        "address_line_2": "",
        "city": "Murfreesboro",
        "state": "TN",
        "assigned_employee": "Andrew Crowder",
        "price": "$129.00",
        "notes": "Waiting for capacitor shipment before final replacement and testing.",
        "date_created": "03/20/2026",
    },
    4: {
        "customer_id": "",
        "customer_name": "Marcus Hill",
        "job_type": "Filter Replacement",
        "status": "Completed",
        "scheduled_date": "03/18/2026",
        "address_line_1": "",
        "address_line_2": "",
        "city": "Brentwood",
        "state": "TN",
        "assigned_employee": "James Whitfield",
        "price": "$49.99",
        "notes": "Completed routine filter replacement and airflow verification.",
        "date_created": "03/17/2026",
    },
}

DEFAULT_JOB = {
    "customer_id": "",
    "customer_name": "Unknown",
    "job_type": "",
    "status": "",
    "scheduled_date": "",
    "address_line_1": "",
    "address_line_2": "",
    "city": "",
    "state": "",
    "assigned_employee": "",
    "price": "",
    "notes": "",
    "date_created": "",
}

MOCKED_SERVICES = [
    {
        "service_type": "Spring Tune-Up",
        "service_default_price": "$199.99",
    },
    {
        "service_type": "Blower Motor Replacement",
        "service_default_price": "$499.99",
    },
    {
        "service_type": "Capacitor Replacement",
        "service_default_price": "$129.00",
    },
    {
        "service_type": "Filter Replacement",
        "service_default_price": "$49.99",
    },
]


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/customers")
def customers():
    return render_template("pages/customers.html")


@app.route("/customers/add")
def add_customer():
    return render_template("pages/add_customer.html")


@app.route("/jobs")
def jobs():
    return render_template("pages/jobs.html")


@app.route("/services")
def manage_services():
    return render_template(
        "pages/manage_services.html",
        services=MOCKED_SERVICES,
    )


@app.route("/services/create", methods=["GET", "POST"])
def create_service():
    if request.method == "POST":
        service_name = request.form.get("service", "").strip()
        service_default_price = request.form.get("service_default_price", "").strip()

        if service_name and service_default_price:
            MOCKED_SERVICES.append(
                {
                    "service_type": service_name,
                    "service_default_price": service_default_price,
                }
            )

        return redirect(url_for("manage_services"))

    return render_template("pages/create_service.html")


@app.route("/services/<int:serviceId>")
def view_service(serviceId):
    # Stubbed service lookup until database integration is added.
    service = MOCKED_SERVICES[serviceId] if 0 <= serviceId < len(MOCKED_SERVICES) else None
    if not service:
        return redirect(url_for("manage_services"))

    return render_template(
        "pages/view_service.html",
        serviceId=serviceId,
        service=service,
    )


@app.route("/customers/<int:customerId>")
def view_customer(customerId):
    # Stubbed customer lookup until database integration is added.
    customer = MOCKED_CUSTOMERS.get(customerId, DEFAULT_CUSTOMER)
    customer_pages = MOCKED_CUSTOMER_PAGES.get(customerId, DEFAULT_CUSTOMER_PAGES)

    return render_template(
        "pages/view_customer.html",
        customerId=customerId,
        customer=customer,
        customer_pages=customer_pages,
    )


@app.route("/customers/<int:customerId>/jobs/create")
def create_job(customerId):
    customer = MOCKED_CUSTOMERS.get(customerId, DEFAULT_CUSTOMER)

    return render_template(
        "pages/create_job.html",
        customerId=customerId,
        customer=customer,
    )


@app.route("/jobs/<int:jobId>")
def view_job(jobId):
    # Stubbed job lookup until database integration is added.
    job = MOCKED_JOBS.get(jobId, DEFAULT_JOB)
    customer = MOCKED_CUSTOMERS.get(job.get("customer_id"), DEFAULT_CUSTOMER)

    return render_template(
        "pages/view_job.html",
        jobId=jobId,
        job=job,
        customer=customer,
    )


@app.route("/jobs/<int:jobId>/complete", methods=["POST"])
def complete_job(jobId):
    # Get job and customer data
    job = MOCKED_JOBS.get(jobId, DEFAULT_JOB)
    customer = MOCKED_CUSTOMERS.get(job.get("customer_id"), DEFAULT_CUSTOMER)

    # Generate invoice PDF
    invoice_path = generate_invoice(jobId, job, customer)

    # Update job status to Completed
    if jobId in MOCKED_JOBS:
        MOCKED_JOBS[jobId]["status"] = "Completed"

    # Redirect back to the job view
    return redirect(url_for("view_job", jobId=jobId))


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
