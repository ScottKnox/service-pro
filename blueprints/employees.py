from datetime import UTC, datetime
import os
import re
from io import BytesIO

from bson import ObjectId
from flask import Blueprint, Response, current_app, redirect, render_template, request, session, url_for
from PIL import Image
from werkzeug.security import generate_password_hash

from mongo import ensure_connection_or_500, object_id_or_404, reference_value, serialize_doc
from utils.csv_export import build_csv_export_response
from utils import object_storage

bp = Blueprint("employees", __name__)

PASSWORD_REQUIREMENTS_MESSAGE = (
    "Password must be at least 8 characters and include at least one uppercase letter, "
    "one number, and one special character from !@#$%^&*."
)
PASSWORD_REQUIREMENTS_PATTERN = re.compile(r"^(?=.*[A-Z])(?=.*\d)(?=.*[!@#$%^&*]).{8,}$")
EMAIL_VALIDATION_MESSAGE = "Enter a valid email address."
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
ALLOWED_PROFILE_PHOTO_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_PROFILE_PHOTO_FILE_SIZE = 2 * 1024 * 1024
MIN_PROFILE_PHOTO_WIDTH = 300
MIN_PROFILE_PHOTO_HEIGHT = 100
MAX_PROFILE_PHOTO_WIDTH = 2400
MAX_PROFILE_PHOTO_HEIGHT = 1400

_PROFILE_PHOTO_MIME_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


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


def _is_authenticated_employee():
    employee_id = session.get("employee_id")
    return bool(employee_id and ObjectId.is_valid(employee_id))


def _employee_has_access_to_employee(db, target_employee_id):
    """Check if authenticated employee can access another employee (must be same business)."""
    if not _is_authenticated_employee():
        return False

    if not target_employee_id or not ObjectId.is_valid(str(target_employee_id)):
        return False

    employee_business_id = _resolve_current_business_id(db)
    if not employee_business_id:
        return False

    target_employee = db.employees.find_one(
        {"_id": ObjectId(target_employee_id) if isinstance(target_employee_id, str) else target_employee_id},
        {"business": 1}
    )
    if not target_employee:
        return False

    target_business = target_employee.get("business")
    if isinstance(target_business, ObjectId):
        return target_business == employee_business_id
    if isinstance(target_business, str) and ObjectId.is_valid(target_business):
        return ObjectId(target_business) == employee_business_id
    return False


@bp.before_request
def _enforce_staff_employee_scope():
    """Guard employee routes to prevent cross-business access."""
    if not _is_authenticated_employee():
        return None

    view_args = request.view_args or {}
    employee_id = str(view_args.get("employeeId") or "").strip()
    if not employee_id or not ObjectId.is_valid(employee_id):
        return None

    db = ensure_connection_or_500()
    if _employee_has_access_to_employee(db, employee_id):
        return None

    current_app.logger.warning(
        "Blocked cross-business employee access: employee_id=%s target_employee_id=%s",
        str(session.get("employee_id") or ""),
        employee_id,
    )
    return redirect(url_for("employees.employees"))


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


def _is_self(employee_id):
    current_employee_id = str(session.get("employee_id") or "").strip()
    return bool(current_employee_id and current_employee_id == str(employee_id or "").strip())


def _employee_photo_status_payload(status):
    if status == "uploaded":
        return "success", "Profile photo uploaded successfully."
    if status == "deleted":
        return "success", "Profile photo removed successfully."
    if status == "missing":
        return "error", "Please choose a profile photo to upload."
    if status == "invalid_type":
        return "error", "Unsupported file type. Please upload PNG, JPG, JPEG, or WEBP."
    if status == "too_large":
        return "error", "Profile photo file is too large. Maximum allowed size is 2 MB."
    if status == "bad_resolution":
        return "error", "Profile photo resolution is not supported. Use between 300x100 and 2400x1400 pixels."
    if status == "invalid_image":
        return "error", "The selected file is not a valid image."
    if status == "upload_failed":
        return "error", "Unable to upload profile photo. Please try again."
    if status == "forbidden":
        return "error", "You can only manage your own profile photo."
    return "", ""


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
            "created_at": datetime.now(UTC),
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

    serialized_employee = serialize_doc(employee)
    employee_photo_url = ""
    profile_photo = str(serialized_employee.get("profile_photo") or "").strip()
    if profile_photo:
        employee_photo_url = url_for("employees.view_employee_profile_photo", employeeId=employeeId)
        photo_version = str(serialized_employee.get("profile_photo_uploaded_at") or "").strip()
        if photo_version:
            separator = "&" if "?" in employee_photo_url else "?"
            employee_photo_url = f"{employee_photo_url}{separator}v={photo_version}"

    employee_photo_status_kind, employee_photo_status_message = _employee_photo_status_payload(
        request.args.get("photo_status", "")
    )

    return render_template(
        "employees/view_employee.html",
        employeeId=employeeId,
        employee=serialized_employee,
        employee_photo_url=employee_photo_url,
        employee_photo_status_kind=employee_photo_status_kind,
        employee_photo_status_message=employee_photo_status_message,
    )


@bp.route("/employees/<employeeId>/profile-photo/view")
def view_employee_profile_photo(employeeId):
    db = ensure_connection_or_500()
    employee = db.employees.find_one({"_id": object_id_or_404(employeeId)}, {"profile_photo": 1})
    if not employee:
        return Response(status=404)

    photo_key = str((employee or {}).get("profile_photo") or "").strip()
    if not photo_key:
        return Response(status=404)

    photo_bytes = object_storage.download_object_bytes(photo_key)
    if not photo_bytes:
        fallback_url = object_storage.build_access_url(photo_key)
        if fallback_url:
            return redirect(fallback_url)
        return Response(status=404)

    extension = photo_key.rsplit(".", 1)[-1].lower() if "." in photo_key else ""
    mime_type = _PROFILE_PHOTO_MIME_TYPES.get(extension, "image/png")
    return Response(photo_bytes, mimetype=mime_type, headers={"Cache-Control": "private, max-age=300"})


@bp.route("/employees/<employeeId>/profile-photo", methods=["POST"])
def upload_profile_photo(employeeId):
    if not _is_self(employeeId):
        return redirect(url_for("employees.view_employee", employeeId=employeeId, photo_status="forbidden"))

    db = ensure_connection_or_500()
    employee_oid = object_id_or_404(employeeId)
    employee = db.employees.find_one({"_id": employee_oid}, {"_id": 1})
    if not employee:
        return redirect(url_for("employees.employees"))

    photo_file = request.files.get("employee_profile_photo_file")
    if not photo_file or not str(photo_file.filename or "").strip():
        return redirect(url_for("employees.view_employee", employeeId=employeeId, photo_status="missing"))

    filename = str(photo_file.filename or "").strip()
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in ALLOWED_PROFILE_PHOTO_EXTENSIONS:
        return redirect(url_for("employees.view_employee", employeeId=employeeId, photo_status="invalid_type"))

    photo_file.stream.seek(0, os.SEEK_END)
    file_size = photo_file.stream.tell()
    photo_file.stream.seek(0)
    if file_size > MAX_PROFILE_PHOTO_FILE_SIZE:
        return redirect(url_for("employees.view_employee", employeeId=employeeId, photo_status="too_large"))

    try:
        with Image.open(photo_file.stream) as image_file:
            image_file.load()
            width, height = image_file.size
            if (
                width < MIN_PROFILE_PHOTO_WIDTH
                or height < MIN_PROFILE_PHOTO_HEIGHT
                or width > MAX_PROFILE_PHOTO_WIDTH
                or height > MAX_PROFILE_PHOTO_HEIGHT
            ):
                return redirect(url_for("employees.view_employee", employeeId=employeeId, photo_status="bad_resolution"))

            save_image = image_file.convert("RGBA")
            save_image.thumbnail((1200, 600), Image.Resampling.LANCZOS)

            encoded = BytesIO()
            save_image.save(encoded, format="PNG", optimize=True)
            object_key = f"employee-profile-photos/{employeeId}_profile.png"
            object_storage.upload_bytes(
                object_key=object_key,
                data=encoded.getvalue(),
                content_type="image/png",
            )
    except Exception:
        return redirect(url_for("employees.view_employee", employeeId=employeeId, photo_status="upload_failed"))

    db.employees.update_one(
        {"_id": employee_oid},
        {
            "$set": {
                "profile_photo": object_key,
                "profile_photo_uploaded_at": datetime.now(UTC),
            }
        },
    )

    return redirect(url_for("employees.view_employee", employeeId=employeeId, photo_status="uploaded"))


@bp.route("/employees/<employeeId>/profile-photo/delete", methods=["POST"])
def delete_profile_photo(employeeId):
    if not _is_self(employeeId):
        return redirect(url_for("employees.view_employee", employeeId=employeeId, photo_status="forbidden"))

    db = ensure_connection_or_500()
    employee_oid = object_id_or_404(employeeId)
    employee = db.employees.find_one({"_id": employee_oid}, {"profile_photo": 1})
    if not employee:
        return redirect(url_for("employees.employees"))

    profile_photo_key = str((employee or {}).get("profile_photo") or "").strip()
    if profile_photo_key:
        object_storage.delete_object(profile_photo_key)

    db.employees.update_one(
        {"_id": employee_oid},
        {
            "$unset": {
                "profile_photo": "",
                "profile_photo_uploaded_at": "",
            }
        },
    )

    return redirect(url_for("employees.view_employee", employeeId=employeeId, photo_status="deleted"))


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
