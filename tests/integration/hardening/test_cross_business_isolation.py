"""Test cross-business data isolation - verify one business cannot access another's data."""

from bson import ObjectId

from tests.integration.factories.customers import seed_customer, seed_customer_with_related_records


def test_staff_cannot_access_customer_from_different_business(test_app, mongo_db):
    """Verify staff member cannot view customer from different business."""
    # Create two businesses
    business1_id = ObjectId()
    business2_id = ObjectId()
    employee1_id = ObjectId()
    employee2_id = ObjectId()

    mongo_db.businesses.insert_one({"_id": business1_id, "company_name": "Business 1"})
    mongo_db.businesses.insert_one({"_id": business2_id, "company_name": "Business 2"})

    mongo_db.employees.insert_one({
        "_id": employee1_id,
        "first_name": "Employee",
        "last_name": "One",
        "business": business1_id,
    })
    mongo_db.employees.insert_one({
        "_id": employee2_id,
        "first_name": "Employee",
        "last_name": "Two",
        "business": business2_id,
    })

    # Create customer in business1
    customer_business1 = seed_customer(mongo_db, business_id=business1_id)
    customer_id = customer_business1["_id"]

    # Employee from business1 CAN access
    client = test_app.test_client()
    with client.session_transaction() as sess:
        sess["employee_id"] = str(employee1_id)
        sess["employee_business_id"] = str(business1_id)

    response = client.get(f"/customers/{customer_id}", follow_redirects=False)
    assert response.status_code == 200, "Employee from same business should access customer"

    # Employee from business2 CANNOT access
    client = test_app.test_client()
    with client.session_transaction() as sess:
        sess["employee_id"] = str(employee2_id)
        sess["employee_business_id"] = str(business2_id)

    response = client.get(f"/customers/{customer_id}", follow_redirects=False)
    assert response.status_code == 302, "Employee from different business should be redirected"
    assert "/customers" in response.headers["Location"], "Should redirect to customers list"


def test_staff_cannot_access_employee_from_different_business(test_app, mongo_db):
    """Verify staff member cannot view employee from different business."""
    business1_id = ObjectId()
    business2_id = ObjectId()
    employee1_id = ObjectId()
    employee2_id = ObjectId()
    target_employee_id = ObjectId()

    mongo_db.businesses.insert_one({"_id": business1_id, "company_name": "Business 1"})
    mongo_db.businesses.insert_one({"_id": business2_id, "company_name": "Business 2"})

    mongo_db.employees.insert_one({
        "_id": employee1_id,
        "first_name": "Employee",
        "last_name": "One",
        "business": business1_id,
    })
    mongo_db.employees.insert_one({
        "_id": employee2_id,
        "first_name": "Employee",
        "last_name": "Two",
        "business": business2_id,
    })
    mongo_db.employees.insert_one({
        "_id": target_employee_id,
        "first_name": "Target",
        "last_name": "Employee",
        "business": business1_id,
    })

    # DEBUG: Verify database state
    emp1 = mongo_db.employees.find_one({"_id": employee1_id})
    target_emp = mongo_db.employees.find_one({"_id": target_employee_id})
    print(f"Employee1 business: {emp1.get('business')}, target business: {target_emp.get('business')}")
    print(f"Business1_id: {business1_id}")

    # Employee from business1 CAN access target in business1
    client = test_app.test_client()
    with client.session_transaction() as sess:
        sess["employee_id"] = str(employee1_id)
        sess["employee_business_id"] = str(business1_id)

    response = client.get(f"/employees/{target_employee_id}", follow_redirects=False)
    print(f"Response status: {response.status_code}")
    assert response.status_code == 200, f"Same business employees should be accessible, got {response.status_code}"

    # Employee from business2 CANNOT access target in business1
    client = test_app.test_client()
    with client.session_transaction() as sess:
        sess["employee_id"] = str(employee2_id)
        sess["employee_business_id"] = str(business2_id)

    response = client.get(f"/employees/{target_employee_id}", follow_redirects=False)
    assert response.status_code == 302, "Different business employees should be blocked"


def test_staff_cannot_access_catalog_item_from_different_business(test_app, mongo_db):
    """Verify staff member cannot view catalog items from different business."""
    business1_id = ObjectId()
    business2_id = ObjectId()
    employee1_id = ObjectId()
    employee2_id = ObjectId()
    service_id = ObjectId()

    mongo_db.businesses.insert_one({"_id": business1_id, "company_name": "Business 1"})
    mongo_db.businesses.insert_one({"_id": business2_id, "company_name": "Business 2"})

    mongo_db.employees.insert_one({
        "_id": employee1_id,
        "first_name": "Employee",
        "last_name": "One",
        "business": business1_id,
    })
    mongo_db.employees.insert_one({
        "_id": employee2_id,
        "first_name": "Employee",
        "last_name": "Two",
        "business": business2_id,
    })
    mongo_db.services.insert_one({
        "_id": service_id,
        "service_name": "AC Maintenance",
        "business_id": business1_id,
    })

    # Employee from business1 CAN access service
    client = test_app.test_client()
    with client.session_transaction() as sess:
        sess["employee_id"] = str(employee1_id)
        sess["employee_business_id"] = str(business1_id)

    response = client.get(f"/services/{service_id}", follow_redirects=False)
    assert response.status_code == 200, "Same business services should be accessible"

    # Employee from business2 CANNOT access service
    client = test_app.test_client()
    with client.session_transaction() as sess:
        sess["employee_id"] = str(employee2_id)
        sess["employee_business_id"] = str(business2_id)

    response = client.get(f"/services/{service_id}", follow_redirects=False)
    assert response.status_code == 302, "Different business services should be blocked"
