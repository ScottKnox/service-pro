import importlib
import re

import pytest
from bson import ObjectId
from werkzeug.security import check_password_hash


@pytest.fixture
def auth_client(test_app, mongo_db, monkeypatch):
    auth_module = importlib.import_module("blueprints.auth")
    monkeypatch.setattr(auth_module, "ensure_connection_or_500", lambda: mongo_db)

    sent_emails = []

    def fake_send_email(subject, recipients, body, business=None, **kwargs):
        sent_emails.append({"subject": subject, "recipients": recipients, "body": body})
        return True

    monkeypatch.setattr(auth_module, "send_email", fake_send_email)

    with test_app.test_client() as client:
        client.sent_emails = sent_emails
        yield client


def _seed_employee(mongo_db, email="tech@example.com"):
    business_id = ObjectId()
    employee_id = ObjectId()
    mongo_db.businesses.insert_one({"_id": business_id, "company_name": "Reset Test HVAC"})
    mongo_db.employees.insert_one(
        {
            "_id": employee_id,
            "first_name": "Reset",
            "last_name": "Tester",
            "username": "resettester",
            "email": email,
            "password": "old-hash",
            "business": business_id,
        }
    )
    return employee_id


def _extract_token(body):
    match = re.search(r"/reset-password/([A-Za-z0-9_\-]+)", body)
    return match.group(1) if match else None


def test_forgot_password_sends_email_and_creates_token(auth_client, mongo_db):
    employee_id = _seed_employee(mongo_db)

    response = auth_client.post("/forgot-password", data={"email": "tech@example.com"})

    assert response.status_code == 200
    assert len(auth_client.sent_emails) == 1
    record = mongo_db.password_resets.find_one({"employee_id": employee_id})
    assert record is not None
    assert record["used"] is False
    assert "token_hash" in record


def test_forgot_password_unknown_email_is_generic(auth_client, mongo_db):
    response = auth_client.post("/forgot-password", data={"email": "nobody@example.com"})

    assert response.status_code == 200
    assert len(auth_client.sent_emails) == 0
    assert mongo_db.password_resets.count_documents({}) == 0
    assert b"a password reset link has been sent" in response.data


def test_reset_password_happy_path(auth_client, mongo_db):
    employee_id = _seed_employee(mongo_db)
    auth_client.post("/forgot-password", data={"email": "tech@example.com"})
    token = _extract_token(auth_client.sent_emails[0]["body"])
    assert token

    response = auth_client.post(
        f"/reset-password/{token}",
        data={"password": "NewPass1!", "confirm_password": "NewPass1!"},
    )

    assert response.status_code == 302
    assert "reset=success" in response.headers["Location"]
    employee = mongo_db.employees.find_one({"_id": employee_id})
    assert check_password_hash(employee["password"], "NewPass1!")
    record = mongo_db.password_resets.find_one({"employee_id": employee_id})
    assert record["used"] is True


def test_reset_password_token_is_single_use(auth_client, mongo_db):
    _seed_employee(mongo_db)
    auth_client.post("/forgot-password", data={"email": "tech@example.com"})
    token = _extract_token(auth_client.sent_emails[0]["body"])

    auth_client.post(
        f"/reset-password/{token}",
        data={"password": "NewPass1!", "confirm_password": "NewPass1!"},
    )
    second = auth_client.get(f"/reset-password/{token}")

    assert b"invalid or has expired" in second.data


def test_reset_password_rejects_weak_password(auth_client, mongo_db):
    _seed_employee(mongo_db)
    auth_client.post("/forgot-password", data={"email": "tech@example.com"})
    token = _extract_token(auth_client.sent_emails[0]["body"])

    response = auth_client.post(
        f"/reset-password/{token}",
        data={"password": "weak", "confirm_password": "weak"},
    )

    assert response.status_code == 200
    assert b"Password must be at least 8 characters" in response.data


def test_reset_password_invalid_token_shows_error(auth_client, mongo_db):
    _seed_employee(mongo_db)

    response = auth_client.get("/reset-password/not-a-real-token")

    assert b"invalid or has expired" in response.data


def test_forgot_password_cooldown_prevents_resend(auth_client, mongo_db):
    employee_id = _seed_employee(mongo_db)

    auth_client.post("/forgot-password", data={"email": "tech@example.com"})
    auth_client.post("/forgot-password", data={"email": "tech@example.com"})

    # The second immediate request is within the cooldown window.
    assert len(auth_client.sent_emails) == 1
    assert mongo_db.password_resets.count_documents({"employee_id": employee_id, "used": False}) == 1


def test_forgot_password_rate_limits_by_ip(mongo_db):
    auth_module = importlib.import_module("blueprints.auth")
    limit = auth_module.PASSWORD_RESET_MAX_ATTEMPTS_PER_IP

    results = [
        auth_module._reset_requests_are_rate_limited(mongo_db, "203.0.113.5")
        for _ in range(limit + 1)
    ]

    assert results[:limit] == [False] * limit
    assert results[limit] is True

