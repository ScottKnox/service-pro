import os

from PIL import Image
from bson import ObjectId
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for
import stripe

from mongo import ensure_connection_or_500, serialize_doc

bp = Blueprint("business", __name__)

ALLOWED_LOGO_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_LOGO_FILE_SIZE = 2 * 1024 * 1024
MIN_LOGO_WIDTH = 300
MIN_LOGO_HEIGHT = 100
MAX_LOGO_WIDTH = 2400
MAX_LOGO_HEIGHT = 1400
LOGO_UPLOAD_SUBDIR = os.path.join("uploads", "logos")


def _configure_stripe_client():
    secret_key = str(os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not secret_key:
        return ""
    stripe.api_key = secret_key
    return secret_key


def _stripe_status_payload(status):
    if status == "connected":
        return "success", "Stripe account connected successfully."
    if status == "refreshed":
        return "error", "Stripe onboarding expired. Please try connecting again."
    if status == "missing_config":
        return "error", "Stripe is not configured. Add STRIPE_SECRET_KEY first."
    if status == "connect_failed":
        return "error", "Unable to start Stripe onboarding. Please try again."
    return "", ""


def _stripe_obj_value(obj, key, default=None):
    if obj is None:
        return default
    try:
        value = getattr(obj, key)
        if value is not None:
            return value
    except Exception:
        pass
    try:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return obj[key]
    except Exception:
        return default


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
    stripe_status_kind, stripe_status_message = _stripe_status_payload(request.args.get("stripe_status", ""))

    stripe_account_id = str(business.get("stripe_account_id") or "").strip()
    stripe_charges_enabled = bool(business.get("stripe_charges_enabled"))
    stripe_payouts_enabled = bool(business.get("stripe_payouts_enabled"))
    stripe_connect_ready = bool(stripe_account_id and stripe_charges_enabled and stripe_payouts_enabled)

    return render_template(
        "business/business_profile.html",
        business=business,
        logo_url=logo_url,
        logo_status_kind=logo_status_kind,
        logo_status_message=logo_status_message,
        stripe_status_kind=stripe_status_kind,
        stripe_status_message=stripe_status_message,
        stripe_account_id=stripe_account_id,
        stripe_charges_enabled=stripe_charges_enabled,
        stripe_payouts_enabled=stripe_payouts_enabled,
        stripe_connect_ready=stripe_connect_ready,
    )


@bp.route("/business/stripe/connect", methods=["POST"])
def connect_stripe_account():
    if not _is_authorized():
        return redirect(url_for("admin_bp.admin"))

    db = ensure_connection_or_500()
    employee, business_oid, business = _business_context(db)
    if not employee:
        session.clear()
        return redirect(url_for("auth.login"))
    if not business_oid or not business:
        return redirect(url_for("error_page", error="no_business"))

    if not _configure_stripe_client():
        return redirect(url_for("business.business_profile", stripe_status="missing_config"))

    company_name = str((business or {}).get("company_name") or (business or {}).get("business_name") or "").strip()
    business_email = str((business or {}).get("email") or "").strip()
    business_website = str((business or {}).get("website") or "").strip()

    try:
        stripe_account_id = str((business or {}).get("stripe_account_id") or "").strip()
        if stripe_account_id:
            account = stripe.Account.retrieve(stripe_account_id)
        else:
            account = stripe.Account.create(
                type="express",
                country=str(os.getenv("STRIPE_COUNTRY") or "US").strip() or "US",
                email=business_email or None,
                business_profile={
                    "name": company_name or None,
                    "url": business_website or None,
                },
                metadata={
                    "business_id": str(business_oid),
                },
            )
            stripe_account_id = str(_stripe_obj_value(account, "id", "") or "").strip()

        if not stripe_account_id:
            current_app.logger.error("Stripe onboarding failed before account link creation: missing account id for business_id=%s", str(business_oid))
            return redirect(url_for("business.business_profile", stripe_status="connect_failed"))

        refresh_url = url_for("business.refresh_stripe_connect", _external=True)
        return_url = url_for("business.complete_stripe_connect", _external=True)
        account_link = stripe.AccountLink.create(
            account=stripe_account_id,
            refresh_url=refresh_url,
            return_url=return_url,
            type="account_onboarding",
        )

        db.businesses.update_one(
            {"_id": business_oid},
            {
                "$set": {
                    "stripe_account_id": stripe_account_id,
                    "stripe_connect_status": "pending",
                }
            },
        )

        return redirect(str(_stripe_obj_value(account_link, "url", "") or url_for("business.business_profile")))
    except Exception as exc:
        current_app.logger.exception("Stripe onboarding failed: business_id=%s error=%s", str(business_oid), exc)
        return redirect(url_for("business.business_profile", stripe_status="connect_failed"))


@bp.route("/business/stripe/refresh")
def refresh_stripe_connect():
    return redirect(url_for("business.business_profile", stripe_status="refreshed"))


@bp.route("/business/stripe/complete")
def complete_stripe_connect():
    if not _is_authorized():
        return redirect(url_for("admin_bp.admin"))

    db = ensure_connection_or_500()
    employee, business_oid, business = _business_context(db)
    if not employee:
        session.clear()
        return redirect(url_for("auth.login"))
    if not business_oid or not business:
        return redirect(url_for("error_page", error="no_business"))
    if not _configure_stripe_client():
        return redirect(url_for("business.business_profile", stripe_status="missing_config"))

    stripe_account_id = str((business or {}).get("stripe_account_id") or "").strip()
    if not stripe_account_id:
        return redirect(url_for("business.business_profile", stripe_status="connect_failed"))

    try:
        account = stripe.Account.retrieve(stripe_account_id)
        db.businesses.update_one(
            {"_id": business_oid},
            {
                "$set": {
                    "stripe_account_id": stripe_account_id,
                    "stripe_charges_enabled": bool(_stripe_obj_value(account, "charges_enabled", False)),
                    "stripe_payouts_enabled": bool(_stripe_obj_value(account, "payouts_enabled", False)),
                    "stripe_details_submitted": bool(_stripe_obj_value(account, "details_submitted", False)),
                    "stripe_connect_status": "connected" if (_stripe_obj_value(account, "charges_enabled", False) and _stripe_obj_value(account, "payouts_enabled", False)) else "pending",
                }
            },
        )
        return redirect(url_for("business.business_profile", stripe_status="connected"))
    except Exception as exc:
        current_app.logger.exception("Stripe connect completion failed: business_id=%s stripe_account_id=%s error=%s", str(business_oid), stripe_account_id, exc)
        return redirect(url_for("business.business_profile", stripe_status="connect_failed"))


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
        warranty_info = request.form.get("warranty_info", "").strip()
        address_line_1 = request.form.get("address_line_1", "").strip()
        address_line_2 = request.form.get("address_line_2", "").strip()
        city = request.form.get("city", "").strip()
        state = request.form.get("state", "").strip().upper()
        zip_code = request.form.get("zip_code", "").strip()
        phone_number = request.form.get("phone_number", "").strip()
        fax_number = request.form.get("fax_number", "").strip()
        email = request.form.get("email", "").strip()
        website = request.form.get("website", "").strip()
        license_number = request.form.get("license_number", "").strip()
        tax_parts = request.form.get("tax_parts", "no").strip().lower()
        tax_parts_rate = request.form.get("tax_parts_rate", "0").strip()
        tax_repair_labor = request.form.get("tax_repair_labor", "no").strip().lower()
        tax_repair_labor_rate = request.form.get("tax_repair_labor_rate", "0").strip()
        tax_installation = request.form.get("tax_installation", "no").strip().lower()
        tax_installation_rate = request.form.get("tax_installation_rate", "0").strip()
        tax_fabrication = request.form.get("tax_fabrication", "no").strip().lower()
        tax_fabrication_rate = request.form.get("tax_fabrication_rate", "0").strip()
        tax_materials = request.form.get("tax_materials", "no").strip().lower()
        tax_materials_rate = request.form.get("tax_materials_rate", "0").strip()
        quote_email_template = request.form.get("quote_email_template", "").strip()
        invoice_email_template = request.form.get("invoice_email_template", "").strip()
        report_email_template = request.form.get("report_email_template", "").strip()
        default_estimate_expiration_days_raw = request.form.get("default_estimate_expiration_days", "").strip()
        default_payment_due_days_raw = request.form.get("default_payment_due_days", "").strip()

        try:
            default_estimate_expiration_days = max(1, int(default_estimate_expiration_days_raw))
        except ValueError:
            default_estimate_expiration_days = 30

        try:
            default_payment_due_days = max(1, int(default_payment_due_days_raw))
        except ValueError:
            default_payment_due_days = 30

        # Preserve legacy top-level tax_rate for compatibility with existing invoice logic
        tax_rate = tax_parts_rate

        db.businesses.update_one(
            {"_id": business_oid},
            {
                "$set": {
                    "company_name": company_name,
                    "warranty_info": warranty_info,
                    "address_line_1": address_line_1,
                    "address_line_2": address_line_2,
                    "city": city,
                    "state": state,
                    "zip_code": zip_code,
                    "phone_number": phone_number,
                    "fax_number": fax_number,
                    "email": email,
                    "website": website,
                    "license_number": license_number,
                    "tax_rate": tax_rate,
                    "tax_parts": tax_parts,
                    "tax_parts_rate": tax_parts_rate,
                    "tax_repair_labor": tax_repair_labor,
                    "tax_repair_labor_rate": tax_repair_labor_rate,
                    "tax_installation": tax_installation,
                    "tax_installation_rate": tax_installation_rate,
                    "tax_fabrication": tax_fabrication,
                    "tax_fabrication_rate": tax_fabrication_rate,
                    "tax_materials": tax_materials,
                    "tax_materials_rate": tax_materials_rate,
                    "quote_email_template": quote_email_template,
                    "invoice_email_template": invoice_email_template,
                    "report_email_template": report_email_template,
                    "default_estimate_expiration_days": default_estimate_expiration_days,
                    "default_payment_due_days": default_payment_due_days,
                }
            },
        )

        return redirect(url_for("business.business_profile"))

    business = db.businesses.find_one({"_id": business_oid})
    if not business:
        return redirect(url_for("error_page", error="no_business"))

    business = serialize_doc(business)
    business["tax_parts"] = str(business.get("tax_parts") or "no").strip().lower()
    business["tax_parts_rate"] = str(business.get("tax_parts_rate") or business.get("tax_rate") or "0").strip()
    business["tax_repair_labor"] = str(business.get("tax_repair_labor") or "no").strip().lower()
    business["tax_repair_labor_rate"] = str(business.get("tax_repair_labor_rate") or "0").strip()
    business["tax_installation"] = str(business.get("tax_installation") or "no").strip().lower()
    business["tax_installation_rate"] = str(business.get("tax_installation_rate") or "0").strip()
    business["tax_fabrication"] = str(business.get("tax_fabrication") or "no").strip().lower()
    business["tax_fabrication_rate"] = str(business.get("tax_fabrication_rate") or "0").strip()
    business["tax_materials"] = str(business.get("tax_materials") or "no").strip().lower()
    business["tax_materials_rate"] = str(business.get("tax_materials_rate") or "0").strip()
    business["address_line_1"] = str(business.get("address_line_1") or "").strip()
    business["address_line_2"] = str(business.get("address_line_2") or "").strip()
    business["city"] = str(business.get("city") or "").strip()
    business["state"] = str(business.get("state") or "").strip().upper()
    business["zip_code"] = str(business.get("zip_code") or "").strip()
    business["phone_number"] = str(business.get("phone_number") or "").strip()
    business["fax_number"] = str(business.get("fax_number") or "").strip()
    business["email"] = str(business.get("email") or "").strip()
    business["website"] = str(business.get("website") or "").strip()
    business["license_number"] = str(business.get("license_number") or "").strip()
    business["warranty_info"] = str(business.get("warranty_info") or "").strip()
    business["report_email_template"] = str(business.get("report_email_template") or "").strip()
    try:
        business["default_estimate_expiration_days"] = max(1, int(business.get("default_estimate_expiration_days") or 30))
    except (TypeError, ValueError):
        business["default_estimate_expiration_days"] = 30
    try:
        business["default_payment_due_days"] = max(1, int(business.get("default_payment_due_days") or 30))
    except (TypeError, ValueError):
        business["default_payment_due_days"] = 30
    return render_template("business/update_business.html", business=business)
