import os

from PIL import Image
from bson import ObjectId
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from mongo import ensure_connection_or_500, serialize_doc

bp = Blueprint("business", __name__)

ALLOWED_LOGO_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_LOGO_FILE_SIZE = 2 * 1024 * 1024
MIN_LOGO_WIDTH = 300
MIN_LOGO_HEIGHT = 100
MAX_LOGO_WIDTH = 2400
MAX_LOGO_HEIGHT = 1400
LOGO_UPLOAD_SUBDIR = os.path.join("uploads", "logos")


def _is_authorized():
    position = (session.get("employee_position") or "").strip().lower()
    return position in ["owner", "co-owner", "manager"]


def _business_context(db):
    employee_id = session.get("employee_id")
    if not employee_id or not ObjectId.is_valid(employee_id):
        session.clear()
        return None, None, None

    employee = db.employees.find_one({"_id": ObjectId(employee_id)})
    if not employee:
        session.clear()
        return None, None, None

    business_ref = employee.get("business")
    if not business_ref:
        return employee, None, None

    business_oid = None
    if isinstance(business_ref, ObjectId):
        business_oid = business_ref
    elif isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
        business_oid = ObjectId(business_ref)

    if not business_oid:
        return employee, None, None

    business = db.businesses.find_one({"_id": business_oid})
    return employee, business_oid, business


def _logo_status_payload(status):
    if status == "uploaded":
        return "success", "Logo uploaded successfully."
    if status == "missing":
        return "error", "Please choose a logo image to upload."
    if status == "invalid_type":
        return "error", "Unsupported file type. Please upload PNG, JPG, JPEG, or WEBP."
    if status == "too_large":
        return "error", "Logo file is too large. Maximum allowed size is 2 MB."
    if status == "bad_resolution":
        return "error", "Logo resolution is not supported. Use between 300x100 and 2400x1400 pixels."
    if status == "invalid_image":
        return "error", "The selected file is not a valid image."
    if status == "upload_failed":
        return "error", "Unable to upload logo. Please try again."
    return "", ""


@bp.route("/business")
def business_profile():
    if not _is_authorized():
        return redirect(url_for("admin_bp.admin"))

    db = ensure_connection_or_500()

    employee, business_oid, business = _business_context(db)
    if not employee:
        session.clear()
        return redirect(url_for("auth.login"))

    if not business:
        return redirect(url_for("error_page", error="no_business"))

    business = serialize_doc(business)

    custom_logo = os.path.basename(str(business.get("custom_logo") or "").strip())
    logo_path = (
        os.path.join(current_app.root_path, "static", LOGO_UPLOAD_SUBDIR, custom_logo)
        if custom_logo
        else ""
    )
    logo_url = ""
    if custom_logo and os.path.exists(logo_path):
        logo_url = url_for("static", filename=f"{LOGO_UPLOAD_SUBDIR}/{custom_logo}")

    logo_status_kind, logo_status_message = _logo_status_payload(request.args.get("logo_status", ""))

    return render_template(
        "business/business_profile.html",
        business=business,
        logo_url=logo_url,
        logo_status_kind=logo_status_kind,
        logo_status_message=logo_status_message,
    )


@bp.route("/business/logo", methods=["POST"])
def upload_logo():
    if not _is_authorized():
        return redirect(url_for("admin_bp.admin"))

    db = ensure_connection_or_500()
    employee, business_oid, business = _business_context(db)
    if not employee:
        session.clear()
        return redirect(url_for("auth.login"))
    if not business_oid or not business:
        return redirect(url_for("error_page", error="no_business"))

    logo_file = request.files.get("custom_logo_file")
    if not logo_file or not str(logo_file.filename or "").strip():
        return redirect(url_for("business.business_profile", logo_status="missing"))

    filename = str(logo_file.filename or "").strip()
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in ALLOWED_LOGO_EXTENSIONS:
        return redirect(url_for("business.business_profile", logo_status="invalid_type"))

    logo_file.stream.seek(0, os.SEEK_END)
    file_size = logo_file.stream.tell()
    logo_file.stream.seek(0)
    if file_size > MAX_LOGO_FILE_SIZE:
        return redirect(url_for("business.business_profile", logo_status="too_large"))

    try:
        with Image.open(logo_file.stream) as image_file:
            image_file.load()
            width, height = image_file.size
            if (
                width < MIN_LOGO_WIDTH
                or height < MIN_LOGO_HEIGHT
                or width > MAX_LOGO_WIDTH
                or height > MAX_LOGO_HEIGHT
            ):
                return redirect(url_for("business.business_profile", logo_status="bad_resolution"))

            image_format = "PNG"
            upload_filename = f"{str(business_oid)}_logo.png"
            logo_upload_dir = os.path.join(current_app.root_path, "static", LOGO_UPLOAD_SUBDIR)
            os.makedirs(logo_upload_dir, exist_ok=True)
            output_path = os.path.join(logo_upload_dir, upload_filename)

            save_image = image_file.convert("RGBA")
            save_image.thumbnail((1200, 600), Image.Resampling.LANCZOS)
            save_image.save(output_path, format=image_format, optimize=True)

    except Exception:
        return redirect(url_for("business.business_profile", logo_status="invalid_image"))

    db.businesses.update_one(
        {"_id": business_oid},
        {"$set": {"custom_logo": upload_filename}},
    )

    return redirect(url_for("business.business_profile", logo_status="uploaded"))


@bp.route("/business/update", methods=["GET", "POST"])
def update_business():
    if not _is_authorized():
        return redirect(url_for("admin_bp.admin"))

    db = ensure_connection_or_500()

    employee_id = session.get("employee_id")
    employee = db.employees.find_one({"_id": ObjectId(employee_id)})
    if not employee:
        session.clear()
        return redirect(url_for("auth.login"))

    business_ref = employee.get("business")
    if not business_ref:
        return redirect(url_for("error_page", error="no_business"))

    business_oid = None
    if isinstance(business_ref, ObjectId):
        business_oid = business_ref
    elif isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
        business_oid = ObjectId(business_ref)

    if not business_oid:
        return redirect(url_for("error_page", error="no_business"))

    if request.method == "POST":
        company_name = request.form.get("company_name", "").strip()
        tax_rate = request.form.get("tax_rate", "0").strip()
        quote_email_template = request.form.get("quote_email_template", "").strip()
        invoice_email_template = request.form.get("invoice_email_template", "").strip()

        db.businesses.update_one(
            {"_id": business_oid},
            {
                "$set": {
                    "company_name": company_name,
                    "tax_rate": tax_rate,
                    "quote_email_template": quote_email_template,
                    "invoice_email_template": invoice_email_template,
                }
            },
        )

        return redirect(url_for("business.business_profile"))

    business = db.businesses.find_one({"_id": business_oid})
    if not business:
        return redirect(url_for("error_page", error="no_business"))

    business = serialize_doc(business)
    return render_template("business/update_business.html", business=business)
