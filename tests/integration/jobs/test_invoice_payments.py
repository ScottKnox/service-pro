import importlib

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