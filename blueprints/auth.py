from bson import ObjectId
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from mongo import ensure_connection_or_500, serialize_doc

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    db = ensure_connection_or_500()
    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not username or not password:
            error = "Username and password are required."
        else:
            employee = db.employees.find_one({"username": username})

            if not employee or not check_password_hash(employee.get("password", ""), password):
                current_app.logger.warning("Failed login attempt for username=%r from %s", username, request.remote_addr)
                error = "Invalid username or password."
            else:
                session["employee_id"] = str(employee["_id"])
                session["employee_name"] = f"{employee.get('first_name', '')} {employee.get('last_name', '')}".strip()
                session["employee_position"] = (employee.get("position") or "").strip()
                current_app.logger.info("Login: employee_id=%s username=%r from %s", session["employee_id"], username, request.remote_addr)
                return redirect(url_for("home"))

    return render_template("auth/login.html", error=error)


@bp.route("/logout")
def logout():
    current_app.logger.info("Logout: employee_id=%s", session.get("employee_id"))
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/profile")
def view_profile():
    db = ensure_connection_or_500()
    employee_id = session.get("employee_id")
    employee = db.employees.find_one({"_id": ObjectId(employee_id)})
    if not employee:
        session.clear()
        return redirect(url_for("auth.login"))

    return render_template("profile/view_profile.html", employee=serialize_doc(employee))

