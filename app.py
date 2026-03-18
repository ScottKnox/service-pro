from flask import Flask, render_template

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


if __name__ == "__main__":
    app.run(debug=True)
