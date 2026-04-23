from datetime import datetime
import re

from bson import ObjectId
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from mongo import ensure_connection_or_500, object_id_or_404, reference_value, serialize_doc
from utils.csv_export import build_csv_export_response

bp = Blueprint("employees", __name__)

PASSWORD_REQUIREMENTS_MESSAGE = (
    "Password must be at least 8 characters and include at least one uppercase letter, "
    "one number, and one special character from !@#$%^&*."
)
PASSWORD_REQUIREMENTS_PATTERN = re.compile(r"^(?=.*[A-Z])(?=.*\d)(?=.*[!@#$%^&*]).{8,}$")
EMAIL_VALIDATION_MESSAGE = "Enter a valid email address."
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _password_meets_requirements(password):
    return bool(PASSWORD_REQUIREMENTS_PATTERN.match(password))


def _email_is_valid(email):
    return bool(EMAIL_PATTERN.match(email))


def _resolve_current_business_id(db):
    employee_id = session.get("employee_id")
    if not employee_id or not ObjectId.is_valid(employee_id):
        return None

    employee = db.employees.find_one({"_id": ObjectId(employee_id)}, {"business": 1})
    business_ref = (employee or {}).get("business")
    if isinstance(business_ref, ObjectId):
        return business_ref
    if isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
        return ObjectId(business_ref)
    return None


@bp.route("/employees")
def employees():
    db = ensure_connection_or_500()
    employees_list = [
        serialize_doc(employee)
        for employee in db.employees.find().sort([("last_name", 1), ("first_name", 1)])
    ]
    return render_template("employees/employees.html", employees=employees_list)


@bp.route("/employees/export/csv")
def export_employees_csv():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business": {"$in": [business_id, str(business_id)]}} if business_id else {"_id": None}
    employees_rows = list(db.employees.find(query).sort([("last_name", 1), ("first_name", 1)]))
    return build_csv_export_response(
        employees_rows,
        "employees_export.csv",
        excluded_fields={"username", "password"},
    )


def _is_authorized():
    """Check if current user has permission to manage employees."""
    position = (session.get("employee_position") or "").strip().lower()
    return position in ["owner", "co-owner", "manager"]


@bp.route("/employees/add", methods=["GET", "POST"])
def add_employee():
    if not _is_authorized():
        return redirect(url_for("employees.employees"))
    
    db = ensure_connection_or_500()
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        position = request.form.get("position", "").strip()
        form_data = request.form.to_dict()

        if not (first_name and last_name and username and password and phone and email and position):
            return render_template(
                "employees/add_employee.html",
                error="First name, last name, username, password, phone, email, and position are required.",
                form_data=form_data,
            )

        if not _password_meets_requirements(password):
            return render_template(
                "employees/add_employee.html",
                error=PASSWORD_REQUIREMENTS_MESSAGE,
                form_data=form_data,
            )

        if not _email_is_valid(email):
            return render_template(
                "employees/add_employee.html",
                error=EMAIL_VALIDATION_MESSAGE,
                form_data=form_data,
            )

        current_employee_id = session.get("employee_id")
        current_employee = None
        if current_employee_id and ObjectId.is_valid(current_employee_id):
            current_employee = db.employees.find_one({"_id": ObjectId(current_employee_id)})

        current_subscription_id = (current_employee or {}).get("subscription_id", "").strip()
        if not current_subscription_id:
            return render_template(
                "employees/add_employee.html",
                error="The current employee is missing a subscription_id. Add that in Mongo and try again.",
                form_data=form_data,
            )
        current_business_ref = (current_employee or {}).get("business")

        employee_count = db.employees.count_documents({}) + 1
        employee = {
            "first_name": first_name,
            "last_name": last_name,
            "username": username,
            "password": generate_password_hash(password, method="scrypt"),
            "phone": phone,
            "email": email,
            "position": position,
            "bio": "",
            "status": "active",
            "date_added": datetime.now().strftime("%m/%d/%Y"),
            "created_at": datetime.utcnow(),
            "employee_id": f"EMP-{employee_count:05d}",
            "subscription_id": current_subscription_id,
            "business": reference_value(current_business_ref),
        }
        inserted = db.employees.insert_one(employee)
        current_app.logger.info(
            "Employee created: id=%s username=%r by employee_id=%s",
            str(inserted.inserted_id),
            username,
            session.get("employee_id"),
        )
        return redirect(url_for("employees.view_employee", employeeId=str(inserted.inserted_id)))

    return render_template("employees/add_employee.html", error="", form_data={})


@bp.route("/employees/<employeeId>")
def view_employee(employeeId):
    db = ensure_connection_or_500()
    employee = db.employees.find_one({"_id": object_id_or_404(employeeId)})
    if not employee:
        return redirect(url_for("employees.employees"))

    return render_template(
        "employees/view_employee.html",
        employeeId=employeeId,
        employee=serialize_doc(employee),
    )


@bp.route("/employees/<employeeId>/update", methods=["GET", "POST"])
def update_employee(employeeId):
    if not _is_authorized():
        return redirect(url_for("employees.employees"))
    
    db = ensure_connection_or_500()
    employee = db.employees.find_one({"_id": object_id_or_404(employeeId)})
    if not employee:
        return redirect(url_for("employees.employees"))

    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not first_name or not last_name or not username:
            return render_template(
                "employees/update_employee.html",
                employeeId=employeeId,
                employee=serialize_doc(employee),
                error="First name, last name, and username are required.",
            )

        update_data = {
            "first_name": first_name,
            "last_name": last_name,
            "username": username,
            "phone": request.form.get("phone", "").strip(),
            "email": request.form.get("email", "").strip(),
            "position": request.form.get("position", "").strip(),
            "bio": request.form.get("bio", "").strip(),
            "status": request.form.get("status", "").strip() or "active",
        }
        if password:
            update_data["password"] = generate_password_hash(password, method="scrypt")

        db.employees.update_one({"_id": ObjectId(employeeId)}, {"$set": update_data})
        return redirect(url_for("employees.view_employee", employeeId=employeeId))

    return render_template(
        "employees/update_employee.html",
        employeeId=employeeId,
        employee=serialize_doc(employee),
        error="",
    )


@bp.route("/employees/<employeeId>/delete", methods=["POST"])
def delete_employee(employeeId):
    if not _is_authorized():
        return redirect(url_for("employees.employees"))
    db = ensure_connection_or_500()
    employee_oid = object_id_or_404(employeeId)
    employee = db.employees.find_one({"_id": employee_oid})
    if not employee:
        return redirect(url_for("employees.employees"))

    db.employees.delete_one({"_id": employee_oid})
    current_app.logger.info("Employee deleted: id=%s by employee_id=%s", employeeId, session.get("employee_id"))
    return redirect(url_for("employees.employees"))
