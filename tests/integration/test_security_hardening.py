import importlib

from bson import ObjectId


def _login(client, position):
    with client.session_transaction() as sess:
        sess["employee_id"] = str(ObjectId())
        sess["employee_position"] = position


# --- Admin role-based access control ---------------------------------------


def test_admin_page_blocks_non_management(test_app):
    with test_app.test_client() as client:
        _login(client, "technician")
        resp = client.get("/admin/price-book/maintenance-plans")
        assert resp.status_code == 403


def test_admin_api_returns_json_403_for_non_management(test_app):
    with test_app.test_client() as client:
        _login(client, "technician")
        resp = client.get("/api/maintenance-plan-templates")
        assert resp.status_code == 403
        assert resp.get_json() == {"success": False, "error": "Forbidden"}


def test_admin_landing_allowed_for_all_employees(test_app):
    with test_app.test_client() as client:
        _login(client, "technician")
        resp = client.get("/admin")
        assert resp.status_code == 200


# --- Security response headers ---------------------------------------------


def test_security_headers_present(test_app):
    with test_app.test_client() as client:
        resp = client.get("/login")
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert "default-src 'self'" in resp.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors 'none'" in resp.headers.get("Content-Security-Policy", "")


# --- Login rate limiting ----------------------------------------------------


def test_login_rate_limited_after_threshold(mongo_db):
    auth_module = importlib.import_module("blueprints.auth")
    ip = "203.0.113.5"

    for _ in range(auth_module.LOGIN_MAX_FAILED_ATTEMPTS_PER_IP):
        assert auth_module._login_is_rate_limited(mongo_db, ip) is False
        auth_module._record_failed_login(mongo_db, ip)

    assert auth_module._login_is_rate_limited(mongo_db, ip) is True

    auth_module._clear_login_attempts(mongo_db, ip)
    assert auth_module._login_is_rate_limited(mongo_db, ip) is False


# --- Twilio webhook signature validation ------------------------------------


def test_twilio_signature_rejected_when_invalid(test_app, monkeypatch):
    jobs_module = importlib.import_module("blueprints.jobs")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret-token")

    with test_app.test_request_context(
        "/twilio/sms/status/abc",
        method="POST",
        data={"MessageSid": "SM1", "MessageStatus": "delivered"},
        headers={"X-Twilio-Signature": "not-a-valid-signature"},
    ):
        assert (
            jobs_module._twilio_request_signature_valid(
                "https://example.com/twilio/sms/status/abc"
            )
            is False
        )


def test_twilio_signature_skipped_when_unconfigured(test_app, monkeypatch):
    jobs_module = importlib.import_module("blueprints.jobs")
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)

    with test_app.test_request_context(
        "/twilio/sms/status/abc",
        method="POST",
        data={},
        headers={},
    ):
        assert (
            jobs_module._twilio_request_signature_valid("https://example.com/x") is True
        )


# --- Cross-tenant (IDOR) scoping -------------------------------------------


def test_doc_belongs_to_current_business(test_app, mongo_db):
    from flask import session

    jobs_module = importlib.import_module("blueprints.jobs")
    business_a = ObjectId()

    with test_app.test_request_context():
        session["employee_business_id"] = str(business_a)

        # Same business is allowed.
        assert (
            jobs_module._doc_belongs_to_current_business(
                mongo_db, {"business_id": str(business_a)}
            )
            is True
        )
        # A document from a different business is blocked.
        assert (
            jobs_module._doc_belongs_to_current_business(
                mongo_db, {"business_id": str(ObjectId())}
            )
            is False
        )
        # A document with no business_id is blocked when a business is resolved.
        assert (
            jobs_module._doc_belongs_to_current_business(mongo_db, {}) is False
        )


def test_doc_scope_allows_when_business_unresolved(test_app, mongo_db):
    jobs_module = importlib.import_module("blueprints.jobs")

    # With no session/business context, the check defers (returns True) rather
    # than locking out, leaving authentication as the gate.
    with test_app.test_request_context():
        assert (
            jobs_module._doc_belongs_to_current_business(
                mongo_db, {"business_id": str(ObjectId())}
            )
            is True
        )

