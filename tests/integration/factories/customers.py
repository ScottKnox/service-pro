from bson import ObjectId


def build_add_customer_form_data(**overrides):
    data = {
        "first_name": "Jane",
        "last_name": "Doe",
        "customer_type": "Residential",
        "phone": "417-555-1212",
        "email": "jane@example.com",
        "address_line_1": "123 Main St",
        "address_line_2": "",
        "city": "Springfield",
        "state": "mo",
        "zip_code": "65807",
        "referral_source": "Web",
    }
    data.update(overrides)
    return data


def build_update_customer_form_data(**overrides):
    data = {
        "first_name": "Lead",
        "last_name": "Customer",
        "company": "",
        "phone": "417-999-0000",
        "email": "lead@example.com",
        "address_line_1": "500 Oak St",
        "address_line_2": "",
        "city": "Springfield",
        "state": "mo",
        "zip_code": "65802",
        "referral_source": "Referral",
    }
    data.update(overrides)
    return data


def make_customer_doc(**overrides):
    document = {
        "_id": ObjectId(),
        "first_name": "Lead",
        "last_name": "Customer",
        "phone": "",
        "email": "",
        "address_line_1": "",
        "address_line_2": "",
        "city": "",
        "state": "",
        "zip_code": "",
        "customer_status": "Lead",
        "properties": [],
    }
    document.update(overrides)
    return document


def seed_customer(db, **overrides):
    customer = make_customer_doc(**overrides)
    db.customers.insert_one(customer)
    return customer


def seed_customer_with_related_records(db, customer_id=None):
    resolved_customer_id = customer_id or ObjectId()
    job_id = ObjectId()

    db.customers.insert_one(
        {
            "_id": resolved_customer_id,
            "first_name": "Delete",
            "last_name": "Me",
        }
    )
    db.jobs.insert_one({"_id": job_id, "customer_id": resolved_customer_id})
    db.equipment.insert_one({"_id": ObjectId(), "customer_id": resolved_customer_id})
    db.estimates.insert_one({"_id": ObjectId(), "customer_id": resolved_customer_id})
    db.estimates.insert_one({"_id": ObjectId(), "job_id": str(job_id)})

    return {
        "customer_id": resolved_customer_id,
        "job_id": job_id,
    }
