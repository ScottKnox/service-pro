import importlib

from bson import ObjectId
from werkzeug.datastructures import MultiDict


def _seed_customer(db, business_id):
    customer_id = ObjectId()
    db.customers.insert_one(
        {
            "_id": customer_id,
            "business_id": business_id,
            "first_name": "Multi",
            "last_name": "Tech",
            "address_line_1": "123 Main St",
            "city": "Springfield",
            "state": "MO",
            "zip_code": "65807",
            "properties": [],
        }
    )
    return customer_id


def _seed_service(db, business_id, service_code="svc-1"):
    service_id = ObjectId()
    db.services.insert_one(
        {
            "_id": service_id,
            "business_id": business_id,
            "service_code": service_code,
            "service_name": "Maintenance Visit",
            "service_type": "Maintenance",
            "standard_price": 150.0,
            "estimated_hours": "2",
        }
    )
    return service_id, service_code


def _seed_employee(db, business_id, first_name, last_name, status="active"):
    employee_id = ObjectId()
    db.employees.insert_one(
        {
            "_id": employee_id,
            "first_name": first_name,
            "last_name": last_name,
            "position": "tech",
            "business": business_id,
            "subscription_id": "",
            "status": status,
        }
    )
    return employee_id


def _build_job_form_data(**overrides):
    data = MultiDict(
        [
            ("job_property_id", ""),
            ("job_date", "2026-05-16"),
            ("job_time", "09:00"),
            ("payment_due_days", "30"),
            ("job_address_line_1", "123 Main St"),
            ("job_address_line_2", ""),
            ("job_city", "Springfield"),
            ("job_state", "MO"),
            ("job_zip_code", "65807"),
            ("invoice_notes", ""),
            ("service_code[]", "svc-1"),
            ("service_price[]", "150.00"),
            ("service_hours[]", "2"),
            ("service_emergency_call[]", "no"),
        ]
    )

    for key, value in overrides.items():
        if isinstance(value, (list, tuple)):
            data.setlist(key, [str(item) for item in value])
        else:
            data.setlist(key, [str(value)])

    return data


def _latest_job(db, customer_id):
    return db.jobs.find_one({"customer_id": customer_id}, sort=[("_id", -1)])


def test_create_job_allows_optional_primary_technician(authed_client, mongo_db):
    business_id = authed_client.business_id
    customer_id = _seed_customer(mongo_db, business_id)
    _seed_service(mongo_db, business_id)

    response = authed_client.post(
        f"/customers/{customer_id}/jobs/create",
        data=_build_job_form_data(),
        follow_redirects=False,
    )

    assert response.status_code == 302

    created_job = _latest_job(mongo_db, customer_id)
    assert created_job is not None
    assert created_job["status"] == "Pending"
    assert created_job.get("primary_technician_id") is None
    assert created_job.get("additional_technician_ids") == []
    assert created_job.get("additional_technician_names") == []
    assert created_job.get("assigned_employee") == ""


def test_create_job_sanitizes_primary_and_assistant_technicians(authed_client, mongo_db):
    jobs_module = importlib.import_module("blueprints.jobs")
    business_id = authed_client.business_id
    customer_id = _seed_customer(mongo_db, business_id)
    _seed_service(mongo_db, business_id)
    primary_employee_id = _seed_employee(mongo_db, business_id, "Primary", "Tech")
    assistant_employee_id = _seed_employee(mongo_db, business_id, "Assistant", "Tech")
    inactive_employee_id = _seed_employee(mongo_db, business_id, "Inactive", "Tech", status="inactive")

    response = authed_client.post(
        f"/customers/{customer_id}/jobs/create",
        data=_build_job_form_data(
            primary_technician_id=str(primary_employee_id),
            **{
                "additional_technician_ids[]": [
                    str(primary_employee_id),
                    str(assistant_employee_id),
                    str(inactive_employee_id),
                ]
            },
        ),
        follow_redirects=False,
    )

    assert response.status_code == 302

    created_job = _latest_job(mongo_db, customer_id)
    assert created_job is not None
    assert created_job["status"] == "Scheduled"
    assert created_job["primary_technician_id"] == str(primary_employee_id)
    assert created_job["assigned_employee"] == "Primary Tech"
    assert created_job["additional_technician_ids"] == [str(assistant_employee_id)]
    assert created_job["additional_technician_names"] == ["Assistant Tech"]

    payload = jobs_module._build_job_technician_payload(
        mongo_db,
        str(primary_employee_id),
        [str(primary_employee_id), str(assistant_employee_id), str(inactive_employee_id)],
    )
    assert payload["primary_technician_id"] == str(primary_employee_id)
    assert payload["additional_technician_ids"] == [str(assistant_employee_id)]
    assert payload["additional_technician_names"] == ["Assistant Tech"]


def test_update_job_preserves_optional_assignment_and_sanitizes_assistants(authed_client, mongo_db):
    business_id = authed_client.business_id
    customer_id = _seed_customer(mongo_db, business_id)
    _seed_service(mongo_db, business_id)
    primary_employee_id = _seed_employee(mongo_db, business_id, "Lead", "Tech")
    assistant_employee_id = _seed_employee(mongo_db, business_id, "Helper", "Tech")
    inactive_employee_id = _seed_employee(mongo_db, business_id, "Ghost", "Tech", status="inactive")

    job_id = ObjectId()
    mongo_db.jobs.insert_one(
        {
            "_id": job_id,
            "business_id": business_id,
            "customer_id": customer_id,
            "customer_name": "Multi Tech",
            "job_type": "Maintenance Visit",
            "status": "Pending",
            "scheduled_date": "",
            "scheduled_time": "",
            "dateScheduled": "",
            "address_line_1": "123 Main St",
            "address_line_2": "",
            "city": "Springfield",
            "state": "MO",
            "zip_code": "65807",
            "services": [],
            "parts": [],
            "labors": [],
            "materials": [],
            "equipments": [],
            "discounts": [],
            "invoices": [],
            "job_kind": "one_time",
            "series_id": None,
            "occurrence_index": None,
        }
    )

    response = authed_client.post(
        f"/jobs/{job_id}/update",
        data=_build_job_form_data(
            primary_technician_id=str(primary_employee_id),
            **{
                "additional_technician_ids[]": [
                    str(primary_employee_id),
                    str(assistant_employee_id),
                    str(inactive_employee_id),
                ]
            },
        ),
        follow_redirects=False,
    )

    assert response.status_code == 302

    updated_job = mongo_db.jobs.find_one({"_id": job_id})
    assert updated_job is not None
    assert updated_job["status"] == "Scheduled"
    assert updated_job["primary_technician_id"] == str(primary_employee_id)
    assert updated_job["assigned_employee"] == "Lead Tech"
    assert updated_job["additional_technician_ids"] == [str(assistant_employee_id)]
    assert updated_job["additional_technician_names"] == ["Helper Tech"]
