from tests.integration.factories.customers import (
    build_add_customer_form_data,
    build_update_customer_form_data,
    seed_customer,
    seed_customer_with_related_records,
)


def test_add_customer_creates_record_and_redirects(authed_client, mongo_db):
    response = authed_client.post(
        "/customers/add",
        data=build_add_customer_form_data(),
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "/customers/" in response.headers["Location"]

    customer = mongo_db.customers.find_one({"email": "jane@example.com"})
    assert customer is not None
    assert customer["customer_status"] == "Active"
    assert customer["state"] == "MO"
    assert isinstance(customer.get("properties"), list)
    assert customer["properties"]


def test_add_customer_requires_first_and_last_name(authed_client, mongo_db):
    email = "missing-names@example.com"
    response = authed_client.post(
        "/customers/add",
        data=build_add_customer_form_data(first_name="", last_name="", email=email),
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert b"First name and last name are required." in response.data
    assert mongo_db.customers.find_one({"email": email}) is None


def test_update_customer_promotes_lead_to_active_when_profile_completed(authed_client, mongo_db):
    customer = seed_customer(mongo_db)
    customer_id = customer["_id"]

    response = authed_client.post(
        f"/customers/{customer_id}/update",
        data=build_update_customer_form_data(),
        follow_redirects=False,
    )

    assert response.status_code == 302

    updated = mongo_db.customers.find_one({"_id": customer_id})
    assert updated is not None
    assert updated["customer_status"] == "Active"
    assert updated["state"] == "MO"


def test_delete_customer_removes_customer_and_related_documents(authed_client, mongo_db):
    seeded = seed_customer_with_related_records(mongo_db)
    customer_id = seeded["customer_id"]
    job_id = seeded["job_id"]

    response = authed_client.post(f"/customers/{customer_id}/delete", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/customers")
    assert mongo_db.customers.find_one({"_id": customer_id}) is None
    assert mongo_db.jobs.count_documents({"customer_id": customer_id}) == 0
    assert mongo_db.equipment.count_documents({"customer_id": customer_id}) == 0
    assert mongo_db.estimates.count_documents({"customer_id": customer_id}) == 0
    assert mongo_db.estimates.count_documents({"job_id": str(job_id)}) == 0
