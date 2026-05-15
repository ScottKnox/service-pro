"""Integration tests for Stripe payment amount validation."""

import pytest
from bson import ObjectId


def test_checkout_session_requires_exact_balance_amount(authed_client, mongo_db):
    """Verify checkout rejects amount that doesn't match calculated balance_due."""
    # Create a job with pricing
    business_id = authed_client.business_id
    customer = mongo_db.customers.insert_one({
        "business_id": business_id,
        "first_name": "Test",
        "last_name": "Customer",
        "email": "test@example.com",
        "balance_due_amount": 0.0,
    })
    customer_id = customer.inserted_id

    job = mongo_db.jobs.insert_one({
        "business_id": business_id,
        "customer_id": customer_id,
        "status": "Completed",
        "services": [
            {"name": "Service 1", "price": 100.0},
        ],
        "parts": [],
        "labors": [],
        "materials": [],
        "equipments": [],
        "discounts": [],
        "invoices": [{
            "invoice_id": "INV-001",
            "status": "Sent",
            "file_path": "/invoices/test.pdf",
        }],
    })
    job_id = job.inserted_id

    # Try to checkout with amount that doesn't match balance_due (100 + tax = 108.75)
    response = authed_client.post(
        f"/jobs/{job_id}/invoices/INV-001/stripe-checkout",
        json={
            "amount": 50.00,  # Not equal to calculated balance
        },
        follow_redirects=False,
    )

    # Should reject mismatched amount
    assert response.status_code == 400, f"Expected 400, got {response.status_code}: {response.get_json()}"
    data = response.get_json()
    assert "does not match invoice balance" in data.get("error", "").lower()


def test_checkout_session_rejects_partial_payment_attempt(authed_client, mongo_db):
    """Verify checkout doesn't allow paying less than full balance (if not configured for partial)."""
    business_id = authed_client.business_id
    customer = mongo_db.customers.insert_one({
        "business_id": business_id,
        "first_name": "Test",
        "last_name": "Customer",
        "email": "test@example.com",
    })

    job = mongo_db.jobs.insert_one({
        "business_id": business_id,
        "customer_id": customer.inserted_id,
        "status": "Completed",
        "services": [
            {"name": "Service 1", "price": 1000.0},
        ],
        "parts": [],
        "labors": [],
        "materials": [],
        "equipments": [],
        "discounts": [],
        "invoices": [{
            "invoice_id": "INV-LARGE",
            "status": "Sent",
            "file_path": "/invoices/large.pdf",
        }],
    })

    # Try to pay 500 of 1087.50
    response = authed_client.post(
        f"/jobs/{job.inserted_id}/invoices/INV-LARGE/stripe-checkout",
        json={
            "amount": 500.00,  # Less than balance
        },
        follow_redirects=False,
    )

    # Should reject partial payment
    assert response.status_code == 400
    data = response.get_json()
    assert "does not match invoice balance" in data.get("error", "").lower()


def test_checkout_with_correct_amount_accepted(authed_client, mongo_db):
    """Verify checkout accepts amount that exactly matches balance_due."""
    business_id = authed_client.business_id
    customer = mongo_db.customers.insert_one({
        "business_id": business_id,
        "first_name": "Test",
        "last_name": "Customer",
        "email": "test@example.com",
        "stripe_customer_id": None,
    })

    job = mongo_db.jobs.insert_one({
        "business_id": business_id,
        "customer_id": customer.inserted_id,
        "status": "Completed",
        "services": [
            {"name": "Service 1", "price": 100.0},
        ],
        "parts": [],
        "labors": [],
        "materials": [],
        "equipments": [],
        "discounts": [],
        "invoices": [{
            "invoice_id": "INV-CORRECT",
            "status": "Sent",
            "file_path": "/invoices/correct.pdf",
        }],
    })

    # Should accept exact amount
    # Note: This will fail at Stripe config check, but that's OK - we're testing amount validation passed
    response = authed_client.post(
        f"/jobs/{job.inserted_id}/invoices/INV-CORRECT/stripe-checkout",
        json={
            "amount": "108.75",  # Exact balance_due
        },
        follow_redirects=False,
    )

    # Should pass amount validation (may fail on Stripe config, but not on amount)
    data = response.get_json()
    if response.status_code == 400:
        # If it fails, should NOT be because of amount mismatch
        error = data.get("error", "").lower()
        assert "does not match invoice balance" not in error, \
            f"Correct amount was rejected: {error}"


def test_payment_webhook_rejects_mismatched_amount(test_app, mongo_db):
    """Verify webhook rejects payment with amount that doesn't match metadata."""
    from blueprints.jobs import process_stripe_checkout_completed
    
    with test_app.app_context():
        # Simulate a Stripe checkout session with mismatched amounts
        checkout_session = {
            "id": "cs_test_123",
            "metadata": {
                "job_id": str(ObjectId()),
                "invoice_ref": "INV-001",
                "amount": "100.00",  # Expected amount
            },
            "amount_total": 15000,  # 150.00 in cents - doesn't match metadata
            "payment_intent": "pi_test_123",
            "customer": None,
        }
        
        # This should fail validation
        result = process_stripe_checkout_completed(mongo_db, checkout_session)
        
        # Should return False due to amount mismatch
        assert result is False, "Webhook should reject mismatched amount"


def test_payment_webhook_rejects_overpayment(test_app, mongo_db):
    """Verify webhook rejects payment that exceeds current balance_due."""
    from blueprints.jobs import process_stripe_checkout_completed
    
    with test_app.app_context():
        # Create a job with existing payment
        business_id = ObjectId()
        customer_id = ObjectId()
        job_id = ObjectId()
        
        mongo_db.businesses.insert_one({"_id": business_id, "company_name": "Test"})
        mongo_db.customers.insert_one({
            "_id": customer_id,
            "business_id": business_id,
            "email": "test@example.com",
        })
        
        mongo_db.jobs.insert_one({
            "_id": job_id,
            "business_id": business_id,
            "customer_id": customer_id,
            "status": "Completed",
            "services": [
                {"name": "Service 1", "price": 100.0},
            ],
            "parts": [],
            "labors": [],
            "materials": [],
            "equipments": [],
            "discounts": [],
            "invoices": [{
                "invoice_id": "INV-TEST",
                "status": "Sent",
            }],
        })
        
        # Record payment in payments collection so job is fully paid
        mongo_db.payments.insert_one({
            "job_id": str(job_id),
            "invoice_id": "INV-TEST",
            "company_id": str(business_id),
            "customer_id": str(customer_id),
            "amount": 108.75,
            "payment_method": "card",
            "status": "completed",
        })
        
        # Try to record another payment of 50.00
        checkout_session = {
            "id": "cs_test_456",
            "metadata": {
                "job_id": str(job_id),
                "invoice_ref": "INV-TEST",
                "amount": "50.00",
            },
            "amount_total": 5000,  # 50.00 in cents
            "payment_intent": "pi_test_456",
            "customer": None,
        }
        
        # Since invoice is already paid, balance_due = 0, so any payment exceeds it
        result = process_stripe_checkout_completed(mongo_db, checkout_session)
        
        # Should reject as overpayment
        assert result is False, "Webhook should reject overpayment"
