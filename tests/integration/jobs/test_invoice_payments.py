import importlib
import hashlib
import os

from bson import ObjectId


def test_checkout_completion_uses_stored_session_mapping_when_metadata_is_missing(test_app, mongo_db):
    jobs_module = importlib.import_module("blueprints.jobs")

    customer_id = ObjectId()
    job_id = ObjectId()
    session_id = "cs_test_session_123"

    mongo_db.customers.insert_one(
        {
            "_id": customer_id,
            "balance_due_amount": 275.0,
            "balance_due": "$275.00",
        }
    )
    mongo_db.jobs.insert_one(
        {
            "_id": job_id,
            "customer_id": customer_id,
            "status": "Completed",
            "total_amount": 275.0,
            "total_amount_paid": 0.0,
            "balance_due": 275.0,
            "payment_status": "pending_paid",
            "invoices": [
                {
                    "invoice_id": "INV-SESSION-1",
                    "invoice_number": "INV-1001",
                    "status": "Sent",
                }
            ],
        }
    )

    checkout_session = {
        "id": session_id,
        "metadata": {},
        "amount_total": 27500,
        "payment_intent": "pi_test_123",
        "client_reference_id": f"{job_id}:INV-SESSION-1",
    }

    with test_app.app_context():
        finalized = jobs_module.process_stripe_checkout_completed(mongo_db, checkout_session)

    assert finalized is True

    updated_job = mongo_db.jobs.find_one({"_id": job_id})
    assert updated_job is not None
    assert updated_job["status"] == "Paid"
    assert updated_job["payment_status"] == "paid"
    assert updated_job["total_amount_paid"] == 275.0
    assert updated_job["balance_due"] == 0.0
    updated_invoice = updated_job["invoices"][0]
    assert updated_invoice["status"] == "Paid"

    payment_doc = mongo_db.payments.find_one({"job_id": job_id})
    assert payment_doc is not None
    assert payment_doc["invoice_id"] == "INV-SESSION-1"
    assert payment_doc["amount"] == 275.0
    assert payment_doc["payment_method"] == "card"
    assert payment_doc["stripe_payment_intent_id"] == "pi_test_123"
    assert payment_doc["status"] == "completed"

    updated_customer = mongo_db.customers.find_one({"_id": customer_id})
    assert updated_customer is not None
    assert updated_customer["balance_due_amount"] == 0.0
    assert updated_customer["balance_due"] == "$0.00"


def test_staff_cannot_access_other_business_invoice(test_app, mongo_db):
    business_id = ObjectId()
    other_business_id = ObjectId()
    employee_id = ObjectId()
    job_id = ObjectId()

    mongo_db.businesses.insert_many(
        [
            {"_id": business_id, "company_name": "Authorized HVAC"},
            {"_id": other_business_id, "company_name": "Other HVAC"},
        ]
    )
    mongo_db.employees.insert_one(
        {
            "_id": employee_id,
            "first_name": "Scoped",
            "last_name": "Employee",
            "position": "admin",
            "business": business_id,
            "subscription_id": "",
        }
    )
    mongo_db.jobs.insert_one(
        {
            "_id": job_id,
            "business_id": other_business_id,
            "customer_id": ObjectId(),
            "status": "Completed",
            "invoices": [
                {
                    "invoice_id": "INV-CROSS-1",
                    "invoice_number": "INV-9001",
                    "status": "Sent",
                }
            ],
        }
    )

    with test_app.test_client() as client:
        with client.session_transaction() as sess:
            sess["employee_id"] = str(employee_id)
            sess["employee_name"] = "Scoped Employee"
            sess["employee_position"] = "admin"

        response = client.get(f"/jobs/{job_id}/invoices/INV-CROSS-1", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/jobs")


def test_invoice_pdf_download_requires_valid_access_token(test_app, mongo_db):
    jobs_module = importlib.import_module("blueprints.jobs")
    job_id = ObjectId()
    business_id = ObjectId()
    token_value = "invoice-access-token"
    filename = "test_invoice_access.pdf"
    invoice_path = os.path.join(test_app.root_path, "invoices", filename)
    token_hash = hashlib.sha256(token_value.encode("utf-8")).hexdigest()

    os.makedirs(os.path.dirname(invoice_path), exist_ok=True)
    with open(invoice_path, "wb") as invoice_file:
        invoice_file.write(b"%PDF-1.4\n% test invoice\n")

    mongo_db.businesses.insert_one({"_id": business_id, "company_name": "PDF Test HVAC"})
    mongo_db.jobs.insert_one(
        {
            "_id": job_id,
            "business_id": business_id,
            "customer_id": ObjectId(),
            "status": "Completed",
            "invoices": [
                {
                    "invoice_id": "INV-PDF-1",
                    "invoice_number": "INV-7001",
                    "status": "Sent",
                    "file_path": f"/invoices/{filename}",
                    "access_token_hash": token_hash,
                }
            ],
        }
    )

    try:
        with test_app.test_client() as client:
            invalid_response = client.get(f"/invoices/{filename}?token=wrong-token", follow_redirects=False)
            valid_response = client.get(f"/invoices/{filename}?token={token_value}", follow_redirects=False)

        assert invalid_response.status_code == 403
        assert valid_response.status_code == 200
        assert valid_response.data.startswith(b"%PDF-1.4")
        assert jobs_module._verify_invoice_access_token(
            mongo_db.jobs.find_one({"_id": job_id})["invoices"][0],
            token_value,
        ) is True
    finally:
        try:
            invalid_response.close()
        except Exception:
            pass
        try:
            valid_response.close()
        except Exception:
            pass
        if os.path.exists(invoice_path):
            os.remove(invoice_path)


def test_checkout_session_uses_tax_aware_balance_due_for_max_amount(test_app, mongo_db, monkeypatch):
    jobs_module = importlib.import_module("blueprints.jobs")
    business_id = ObjectId()
    customer_id = ObjectId()
    employee_id = ObjectId()
    job_id = ObjectId()
    captured = {}

    mongo_db.businesses.insert_one(
        {
            "_id": business_id,
            "company_name": "Tax Test HVAC",
            "stripe_account_id": "acct_test_123",
            "stripe_charges_enabled": True,
            "stripe_payouts_enabled": True,
            "tax_rates": [
                {
                    "name": "Sales Tax",
                    "rate": 7.5,
                    "active": True,
                    "applies_to": ["services"],
                }
            ],
        }
    )
    mongo_db.employees.insert_one(
        {
            "_id": employee_id,
            "first_name": "Tax",
            "last_name": "Tester",
            "position": "admin",
            "business": business_id,
            "subscription_id": "",
        }
    )
    mongo_db.customers.insert_one(
        {
            "_id": customer_id,
            "email": "customer@example.com",
            "tax_exempt": False,
        }
    )
    mongo_db.jobs.insert_one(
        {
            "_id": job_id,
            "business_id": business_id,
            "customer_id": customer_id,
            "status": "Completed",
            "total_amount": 100.0,
            "total_amount_paid": 0.0,
            "balance_due": 100.0,
            "payment_status": "pending_paid",
            "services": [
                {
                    "name": "Tune-Up",
                    "price": "$100.00",
                }
            ],
            "invoices": [
                {
                    "invoice_id": "INV-TAX-1",
                    "invoice_number": "INV-3001",
                    "status": "Sent",
                }
            ],
        }
    )

    monkeypatch.setattr(jobs_module, "_configure_stripe_client", lambda: "sk_test_123")

    class _FakeSession:
        url = "https://checkout.stripe.test/session"

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeSession()

    monkeypatch.setattr(jobs_module.stripe.checkout.Session, "create", _fake_create)

    with test_app.test_client() as client:
        with client.session_transaction() as sess:
            sess["employee_id"] = str(employee_id)
            sess["employee_name"] = "Tax Tester"
            sess["employee_position"] = "admin"

        response = client.post(
            f"/jobs/{job_id}/invoices/INV-TAX-1/stripe-checkout",
            json={
                "amount": "107.50",
                "customer_email": "customer@example.com",
            },
        )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert captured["line_items"][0]["price_data"]["unit_amount"] == 10750