from datetime import datetime

from bson import ObjectId
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from mongo import ensure_connection_or_500, object_id_or_404, serialize_doc

bp = Blueprint("employees", __name__)


@bp.route("/employees")
def employees():
    db = ensure_connection_or_500()
    employees_list = [
        serialize_doc(employee)
        for employee in db.employees.find().sort([("last_name", 1), ("first_name", 1)])
    ]
    return render_template("employees/employees.html", employees=employees_list)


@bp.route("/employees/add", methods=["GET", "POST"])
def add_employee():
    db = ensure_connection_or_500()
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if first_name and last_name and username and password:
            employee_count = db.employees.count_documents({}) + 1
            employee = {
                "first_name": first_name,
                "last_name": last_name,
                "username": username,
                "password": generate_password_hash(password, method="scrypt"),
                "phone": request.form.get("phone", "").strip(),
                "email": request.form.get("email", "").strip(),
                "position": request.form.get("position", "").strip(),
                "bio": request.form.get("bio", "").strip(),
                "status": request.form.get("status", "").strip() or "active",
                "date_added": datetime.now().strftime("%m/%d/%Y"),
                "employee_id": f"EMP-{employee_count:05d}",
            }
            inserted = db.employees.insert_one(employee)
            current_app.logger.info(
                "Employee created: id=%s username=%r by employee_id=%s",
                str(inserted.inserted_id),
                username,
                session.get("employee_id"),
            )
            return redirect(url_for("employees.view_employee", employeeId=str(inserted.inserted_id)))

    return render_template("employees/add_employee.html")


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
    db = ensure_connection_or_500()
    employee_oid = object_id_or_404(employeeId)
    employee = db.employees.find_one({"_id": employee_oid})
    if not employee:
        return redirect(url_for("employees.employees"))

    db.employees.delete_one({"_id": employee_oid})
    current_app.logger.info("Employee deleted: id=%s by employee_id=%s", employeeId, session.get("employee_id"))
    return redirect(url_for("employees.employees"))
