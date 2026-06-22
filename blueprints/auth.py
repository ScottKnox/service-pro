from datetime import UTC, datetime, timedelta
import hashlib
import re
import secrets

from bson import ObjectId
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from config import get_notification_base_url
from mongo import build_reference_filter, ensure_connection_or_500, serialize_doc
from utils.notifications import send_email
from utils.security import PASSWORD_REQUIREMENTS_MESSAGE, password_meets_requirements

bp = Blueprint("auth", __name__)

PASSWORD_RESET_TOKEN_TTL_HOURS = 1
# Per-IP throttle: max requests allowed within the rolling window.
PASSWORD_RESET_MAX_ATTEMPTS_PER_IP = 5
PASSWORD_RESET_ATTEMPT_WINDOW_MINUTES = 15
# Per-account cooldown: skip resending if a fresh token was just issued.
PASSWORD_RESET_RESEND_COOLDOWN_SECONDS = 60
# Per-IP login throttle to slow credential brute forcing.
LOGIN_MAX_FAILED_ATTEMPTS_PER_IP = 10
LOGIN_ATTEMPT_WINDOW_MINUTES = 15
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
GENERIC_RESET_NOTICE = (
    "If an account with that email exists, a password reset link has been sent."
)


def _hash_reset_token(raw_token):
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _reset_requests_are_rate_limited(db, ip_address):
    """Record this attempt and return True if the IP has exceeded the limit."""
    now = datetime.now(UTC)
    window_start = now - timedelta(minutes=PASSWORD_RESET_ATTEMPT_WINDOW_MINUTES)
    db.password_reset_attempts.insert_one({"ip": ip_address, "created_at": now})
    recent_attempts = db.password_reset_attempts.count_documents(
        {"ip": ip_address, "created_at": {"$gte": window_start}}
    )
    return recent_attempts > PASSWORD_RESET_MAX_ATTEMPTS_PER_IP


def _login_is_rate_limited(db, ip_address):
    """Return True if this IP has too many recent failed login attempts."""
    window_start = datetime.now(UTC) - timedelta(minutes=LOGIN_ATTEMPT_WINDOW_MINUTES)
    recent_failures = db.login_attempts.count_documents(
        {"ip": ip_address, "created_at": {"$gte": window_start}}
    )
    return recent_failures >= LOGIN_MAX_FAILED_ATTEMPTS_PER_IP


def _record_failed_login(db, ip_address):
    db.login_attempts.insert_one({"ip": ip_address, "created_at": datetime.now(UTC)})


def _clear_login_attempts(db, ip_address):
    db.login_attempts.delete_many({"ip": ip_address})



@bp.route("/login", methods=["GET", "POST"])
def login():
    db = ensure_connection_or_500()
    error = None
    notice = "Your password has been reset. Please log in." if request.args.get("reset") == "success" else None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        client_ip = request.remote_addr or "unknown"

        if _login_is_rate_limited(db, client_ip):
            current_app.logger.warning("Login rate limit hit from %s", client_ip)
            error = "Too many failed login attempts. Please try again later."
        elif not username or not password:
            error = "Username and password are required."
        else:
            employee = db.employees.find_one({"username": username})

            if not employee or not check_password_hash(employee.get("password", ""), password):
                _record_failed_login(db, client_ip)
                current_app.logger.warning("Failed login attempt for username=%r from %s", username, client_ip)
                error = "Invalid username or password."
            else:
                _clear_login_attempts(db, client_ip)
                session["employee_id"] = str(employee["_id"])
                session["employee_name"] = f"{employee.get('first_name', '')} {employee.get('last_name', '')}".strip()
                session["employee_position"] = (employee.get("position") or "").strip()
                current_app.logger.info("Login: employee_id=%s username=%r from %s", session["employee_id"], username, client_ip)
                return redirect(url_for("home"))

    return render_template("auth/login.html", error=error, notice=notice)


@bp.route("/logout")
def logout():
    current_app.logger.info("Logout: employee_id=%s", session.get("employee_id"))
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    db = ensure_connection_or_500()

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()

        if not email or not EMAIL_PATTERN.match(email):
            return render_template(
                "auth/forgot_password.html",
                error="Enter a valid email address.",
                email=email,
            )

        # Throttle by IP to prevent abuse. Over-limit requests still return the
        # same generic message so the limit is not observable to an attacker.
        if _reset_requests_are_rate_limited(db, request.remote_addr or "unknown"):
            current_app.logger.warning("Password reset rate limit hit from %s", request.remote_addr)
            return render_template("auth/forgot_password.html", notice=GENERIC_RESET_NOTICE)

        employee = db.employees.find_one({"email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}})

        if employee:
            now = datetime.now(UTC)
            cooldown_start = now - timedelta(seconds=PASSWORD_RESET_RESEND_COOLDOWN_SECONDS)
            recent_token = db.password_resets.find_one(
                {
                    "employee_id": employee["_id"],
                    "used": False,
                    "created_at": {"$gte": cooldown_start},
                }
            )
            if recent_token is not None:
                # A link was just sent; avoid mailbombing the account.
                current_app.logger.info("Password reset resend skipped (cooldown) for employee_id=%s", str(employee["_id"]))
                return render_template("auth/forgot_password.html", notice=GENERIC_RESET_NOTICE)

            raw_token = secrets.token_urlsafe(32)
            token_hash = _hash_reset_token(raw_token)
            expires_at = now + timedelta(hours=PASSWORD_RESET_TOKEN_TTL_HOURS)

            # Invalidate any prior unused tokens for this employee.
            db.password_resets.update_many(
                {"employee_id": employee["_id"], "used": False},
                {"$set": {"used": True, "invalidated_at": now}},
            )
            db.password_resets.insert_one(
                {
                    "employee_id": employee["_id"],
                    "token_hash": token_hash,
                    "expires_at": expires_at,
                    "used": False,
                    "created_at": now,
                }
            )

            reset_url = f"{get_notification_base_url()}{url_for('auth.reset_password', token=raw_token)}"
            business = db.businesses.find_one(build_reference_filter("_id", employee.get("business"))) or {}
            first_name = (employee.get("first_name") or "").strip() or "there"
            subject = "Reset your Klovent password"
            body = (
                f"Hi {first_name},\n\n"
                "We received a request to reset your Klovent password. "
                "Click the link below to choose a new password:\n\n"
                f"{reset_url}\n\n"
                f"This link expires in {PASSWORD_RESET_TOKEN_TTL_HOURS} hour(s). "
                "If you did not request a password reset, you can safely ignore this email."
            )

            try:
                send_email(subject=subject, recipients=[employee["email"]], body=body, business=business)
                current_app.logger.info("Password reset email sent for employee_id=%s", str(employee["_id"]))
            except Exception as exc:
                current_app.logger.warning("Password reset email failed for employee_id=%s: %s", str(employee["_id"]), exc)
        else:
            current_app.logger.info("Password reset requested for unknown email from %s", request.remote_addr)

        # Always show the same generic message to avoid account enumeration.
        return render_template("auth/forgot_password.html", notice=GENERIC_RESET_NOTICE)

    return render_template("auth/forgot_password.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    db = ensure_connection_or_500()
    token_hash = _hash_reset_token(token or "")
    record = db.password_resets.find_one({"token_hash": token_hash, "used": False})

    invalid = record is None
    if record is not None:
        expires_at = record.get("expires_at")
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at is None or expires_at < datetime.now(UTC):
            invalid = True

    if invalid:
        return render_template("auth/reset_password.html", invalid=True)

    if request.method == "POST":
        password = request.form.get("password", "").strip()
        confirm = request.form.get("confirm_password", "").strip()

        if password != confirm:
            return render_template("auth/reset_password.html", token=token, error="Passwords do not match.")

        if not password_meets_requirements(password):
            return render_template("auth/reset_password.html", token=token, error=PASSWORD_REQUIREMENTS_MESSAGE)

        db.employees.update_one(
            {"_id": record["employee_id"]},
            {"$set": {"password": generate_password_hash(password, method="scrypt")}},
        )
        db.password_resets.update_one(
            {"_id": record["_id"]},
            {"$set": {"used": True, "used_at": datetime.now(UTC)}},
        )
        current_app.logger.info("Password reset completed for employee_id=%s", str(record["employee_id"]))
        return redirect(url_for("auth.login", reset="success"))

    return render_template("auth/reset_password.html", token=token)



@bp.route("/profile")
def view_profile():
    db = ensure_connection_or_500()
    employee_id = session.get("employee_id")
    employee = db.employees.find_one({"_id": ObjectId(employee_id)})
    if not employee:
        session.clear()
        return redirect(url_for("auth.login"))

    return render_template("profile/view_profile.html", employee=serialize_doc(employee))

