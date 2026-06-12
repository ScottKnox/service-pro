import os
import json
import secrets
from io import BytesIO
from datetime import UTC, datetime
from urllib.parse import quote

from PIL import Image
from bson import ObjectId
from flask import Blueprint, Response, current_app, redirect, render_template, request, session, url_for
import stripe

from mongo import ensure_connection_or_500, serialize_doc
from utils.markup import calculate_sell_price, get_markup_rule
from utils import object_storage
from utils.notifications import sms_features_enabled

bp = Blueprint("business", __name__)

ALLOWED_LOGO_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_LOGO_FILE_SIZE = 2 * 1024 * 1024
MIN_LOGO_WIDTH = 300
MIN_LOGO_HEIGHT = 100
MAX_LOGO_WIDTH = 2400
MAX_LOGO_HEIGHT = 1400
DEFAULT_TAX_RATE_TYPES = ["parts", "materials", "equipment"]
INSTALLATION_SERVICE_TYPE = "Installation"
INSTALLATION_PAYMENT_STAGE_OPTIONS = [
    "Deposit",
    "Progress Payment",
    "Materials Payment",
    "Final Payment",
]
INSTALLATION_STAGE_AMOUNT_TYPE_MAP = {
    "Deposit": "percentage",
    "Progress Payment": "fixed",
    "Materials Payment": "fixed",
    "Final Payment": "remaining",
}
INSTALLATION_STAGE_TRIGGER_MAP = {
    "Deposit": "estimate_accepted",
    "Progress Payment": "manual",
    "Materials Payment": "manual",
    "Final Payment": "job_completed",
}
INSTALLATION_TRIGGER_LABEL_MAP = {
    "estimate_accepted": "Estimate accepted",
    "job_completed": "Job completed",
    "manual": "Manual",
}

_LOGO_MIME_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


def _installation_trigger_for_stage_name(stage_name):
    normalized_name = str(stage_name or "").strip()
    return INSTALLATION_STAGE_TRIGGER_MAP.get(normalized_name)


def _installation_amount_type_for_stage_name(stage_name):
    normalized_name = str(stage_name or "").strip()
    return INSTALLATION_STAGE_AMOUNT_TYPE_MAP.get(normalized_name)


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


def _twilio_status_payload(business):
    if not sms_features_enabled():
        return "error", "Twilio SMS is currently disabled by SMS_FEATURES_ENABLED."

    twilio_account_sid = str(os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    twilio_auth_token = str(os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    twilio_phone_number = str((business or {}).get("twilio_phone_number") or "").strip()

    if not twilio_account_sid or not twilio_auth_token:
        return "error", "Twilio SMS is not ready yet. Add TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN on the server."
    if not twilio_phone_number:
        return "error", "Twilio SMS is not ready yet. Add a Twilio Phone Number in Business Profile before sending En Route texts."
    return "success", "Twilio SMS is configured for this business."


def _normalize_tax_rates_for_view(business):
    tax_rates = business.get("tax_rates")
    normalized = []

    if isinstance(tax_rates, list):
        for index, tax_rate in enumerate(tax_rates):
            if not isinstance(tax_rate, dict):
                continue
            name = str(tax_rate.get("name") or "").strip()
            if not name:
                continue
            try:
                rate = float(str(tax_rate.get("rate") or "0").strip() or "0")
            except ValueError:
                rate = 0.0
            applies_to = tax_rate.get("applies_to") if isinstance(tax_rate.get("applies_to"), list) else []
            cleaned_applies_to = []
            for item in applies_to:
                item_name = str(item or "").strip().lower()
                if item_name in ["parts", "materials", "equipment", "services", "labor"] and item_name not in cleaned_applies_to:
                    cleaned_applies_to.append(item_name)
            normalized.append(
                {
                    "name": name,
                    "rate": rate,
                    "agency": str(tax_rate.get("agency") or "").strip(),
                    "active": bool(tax_rate.get("active", True)),
                    "display_order": int(tax_rate.get("display_order") or index),
                    "quickbooks_tax_code": str(tax_rate.get("quickbooks_tax_code") or "").strip(),
                    "applies_to": cleaned_applies_to,
                }
            )

    if normalized:
        return sorted(normalized, key=lambda row: row.get("display_order", 0))

    # Bootstrap sensible defaults when none exist yet.
    return [
        {
            "name": "Missouri Sales Tax",
            "rate": 0.0,
            "agency": "",
            "active": True,
            "display_order": 0,
            "quickbooks_tax_code": "",
            "applies_to": list(DEFAULT_TAX_RATE_TYPES),
        }
    ]


def _is_truthy_form_value(value):
    return str(value or "").strip().lower() in ["true", "1", "yes", "on"]


def _parse_optional_nonnegative_float(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        return None
    if numeric < 0:
        return None
    return numeric


def _normalize_markup_rules_for_view(business):
    rules = business.get("markup_rules")
    if not isinstance(rules, list):
        return []

    normalized = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        try:
            range_min = float(rule.get("range_min") or 0)
            markup_percent = float(rule.get("markup_percent") or 0)
        except (TypeError, ValueError):
            continue
        range_max_raw = rule.get("range_max")
        range_max = None
        if range_max_raw not in [None, ""]:
            try:
                range_max = float(range_max_raw)
            except (TypeError, ValueError):
                range_max = None
        normalized.append(
            {
                "range_min": f"{range_min:.2f}",
                "range_max": "" if range_max is None else f"{range_max:.2f}",
                "markup_percent": f"{markup_percent:.2f}",
            }
        )
    normalized.sort(key=lambda row: float(row.get("range_min") or 0))
    return normalized


def _normalize_payment_schedule_templates_for_view(business):
    raw_templates = business.get("payment_schedule_templates")
    if isinstance(raw_templates, str):
        try:
            raw_templates = json.loads(raw_templates)
        except ValueError:
            raw_templates = []

    if not isinstance(raw_templates, list):
        return []

    normalized_templates = []
    for template_index, template in enumerate(raw_templates):
        if not isinstance(template, dict):
            continue

        category = INSTALLATION_SERVICE_TYPE
        stages = []
        for stage_index, stage in enumerate(template.get("stages") or []):
            if not isinstance(stage, dict):
                continue
            stage_name = str(stage.get("name") or "").strip()
            if stage_name not in INSTALLATION_PAYMENT_STAGE_OPTIONS:
                continue

            amount_type = str(stage.get("amount_type") or "").strip().lower()
            expected_amount_type = _installation_amount_type_for_stage_name(stage_name)
            if amount_type not in {"percentage", "fixed", "remaining"}:
                amount_type = expected_amount_type
            if expected_amount_type:
                amount_type = expected_amount_type

            raw_amount_value = stage.get("amount")
            if amount_type == "remaining":
                amount_value = None
                amount_text = ""
            else:
                if raw_amount_value in [None, ""] and amount_type == "percentage":
                    raw_amount_value = stage.get("percentage")
                try:
                    amount_value = float(raw_amount_value)
                except (TypeError, ValueError):
                    amount_value = 0.0
                amount_value = max(0.0, amount_value)
                if amount_type == "percentage":
                    amount_value = round(amount_value)
                    amount_text = str(int(amount_value))
                else:
                    amount_value = round(amount_value, 2)
                    amount_text = f"{amount_value:.2f}".rstrip("0").rstrip(".")

            trigger_value = _installation_trigger_for_stage_name(stage_name) or "manual"
            stages.append(
                {
                    "stage_key": str(stage.get("stage_key") or "").strip() or f"existing-{template_index}-{stage_index}-{secrets.token_hex(3)}",
                    "name": stage_name,
                    "amount_type": amount_type,
                    "amount": amount_text,
                    "trigger": trigger_value,
                    "trigger_label": INSTALLATION_TRIGGER_LABEL_MAP.get(trigger_value, "Manual"),
                    "send_payment_request": bool(stage.get("send_payment_request", True)),
                }
            )

        normalized_templates.append(
            {
                "template_index": str(template.get("template_index") or template_index),
                "category": category,
                "stages": stages,
            }
        )

    return normalized_templates


def _parse_payment_schedule_templates_from_form(form):
    template_indices = form.getlist("payment_schedule_template_index[]")
    stage_template_indices = form.getlist("payment_schedule_stage_template_index[]")
    stage_keys = form.getlist("payment_schedule_stage_key[]")
    stage_names = form.getlist("payment_schedule_stage_name[]")
    stage_amount_types = form.getlist("payment_schedule_stage_amount_type[]")
    stage_amounts = form.getlist("payment_schedule_stage_amount[]")
    checked_stage_keys = set(form.getlist("payment_schedule_stage_send_request[]"))

    parsed_templates = []
    templates_for_view = []
    template_errors = {}
    template_count = len(template_indices)
    if template_count == 0:
        template_count = 1

    for template_position in range(template_count):
        template_index = str(template_indices[template_position] if template_position < len(template_indices) else template_position).strip() or str(template_position)
        category = INSTALLATION_SERVICE_TYPE
        row_errors = []

        stages = []
        for stage_position in range(len(stage_template_indices)):
            stage_template_index = str(stage_template_indices[stage_position] or "").strip()
            if stage_template_index != template_index:
                continue

            stage_key = str(stage_keys[stage_position] if stage_position < len(stage_keys) else "").strip() or f"{template_index}:{stage_position}"
            stage_name = str(stage_names[stage_position] if stage_position < len(stage_names) else "").strip()
            stage_amount_type_text = str(stage_amount_types[stage_position] if stage_position < len(stage_amount_types) else "").strip().lower()
            stage_amount_text = str(stage_amounts[stage_position] if stage_position < len(stage_amounts) else "").strip()
            if not stage_name and not stage_amount_text:
                continue

            stage_trigger = _installation_trigger_for_stage_name(stage_name)
            expected_amount_type = _installation_amount_type_for_stage_name(stage_name)
            stage_amount_type = expected_amount_type or stage_amount_type_text
            stage_amount_value = None

            if not stage_name:
                row_errors.append("Each stage needs a name.")
            if stage_name and stage_name not in INSTALLATION_PAYMENT_STAGE_OPTIONS:
                row_errors.append("Each stage must use a valid installation stage name.")
            if not stage_trigger:
                row_errors.append("Each stage needs a valid trigger mapping.")
                stage_trigger = "manual"
            if not stage_amount_type:
                row_errors.append("Each stage needs a valid amount type.")

            if stage_amount_type == "percentage":
                try:
                    stage_amount_value = float(stage_amount_text)
                except (TypeError, ValueError):
                    stage_amount_value = None
                if stage_amount_value is None:
                    row_errors.append("Deposit requires a whole-number percentage.")
                else:
                    if stage_amount_value < 0:
                        row_errors.append("Deposit percentage must be zero or greater.")
                    if abs(stage_amount_value - round(stage_amount_value)) > 0.001:
                        row_errors.append("Deposit percentage must be a whole number.")
                    stage_amount_value = round(stage_amount_value)
            elif stage_amount_type == "fixed":
                try:
                    stage_amount_value = float(stage_amount_text)
                except (TypeError, ValueError):
                    stage_amount_value = None
                if stage_amount_value is None:
                    row_errors.append("This stage requires a valid fixed dollar amount.")
                elif stage_amount_value < 0:
                    row_errors.append("Fixed dollar amount must be zero or greater.")
                else:
                    stage_amount_value = round(stage_amount_value, 2)
            elif stage_amount_type == "remaining":
                stage_amount_value = None

            stages.append(
                {
                    "stage_key": stage_key,
                    "name": stage_name,
                    "amount_type": stage_amount_type,
                    "amount": stage_amount_value,
                    "trigger": stage_trigger,
                    "send_payment_request": stage_key in checked_stage_keys,
                }
            )

        if not stages:
            row_errors.append("Add at least one payment stage.")

        stage_names_ordered = [str(stage.get("name") or "").strip() for stage in stages]
        final_payment_indices = [index for index, name in enumerate(stage_names_ordered) if name == "Final Payment"]
        deposit_indices = [index for index, name in enumerate(stage_names_ordered) if name == "Deposit"]

        if len(final_payment_indices) > 1:
            row_errors.append("Only one Final Payment stage is allowed.")
        if len(deposit_indices) > 1:
            row_errors.append("Only one Deposit stage is allowed.")

        if final_payment_indices:
            final_index = final_payment_indices[0]
            if final_index != len(stage_names_ordered) - 1:
                row_errors.append("Final Payment must be the last stage.")
            has_deposit_or_progress_before_final = any(name in {"Deposit", "Progress Payment"} for name in stage_names_ordered[:final_index])
            if not has_deposit_or_progress_before_final:
                row_errors.append("Deposit or Progress Payment must come before Final Payment.")

        if row_errors:
            template_errors[template_index] = row_errors

        parsed_templates.append(
            {
                "template_index": template_index,
                "category": category,
                "stages": [
                    {
                        "name": stage.get("name"),
                        "amount_type": stage.get("amount_type"),
                        "amount": stage.get("amount"),
                        "trigger": stage.get("trigger"),
                        "send_payment_request": bool(stage.get("send_payment_request", True)),
                    }
                    for stage in stages
                ],
            }
        )
        templates_for_view.append(
            {
                "template_index": template_index,
                "category": category,
                "stages": [
                    {
                        "stage_key": stage.get("stage_key"),
                        "name": stage.get("name"),
                        "amount_type": stage.get("amount_type"),
                        "amount": "" if stage.get("amount") is None else f"{float(stage.get('amount') or 0.0):.2f}".rstrip("0").rstrip("."),
                        "trigger": stage.get("trigger"),
                        "trigger_label": INSTALLATION_TRIGGER_LABEL_MAP.get(stage.get("trigger"), "Manual"),
                        "send_payment_request": bool(stage.get("send_payment_request", True)),
                    }
                    for stage in stages
                ],
            }
        )

    cleaned_templates = []
    for template in parsed_templates:
        if not isinstance(template.get("stages"), list) or not template.get("stages"):
            continue
        cleaned_templates.append(
            {
                "category": template.get("category"),
                "stages": template.get("stages"),
            }
        )

    return cleaned_templates, templates_for_view, template_errors


def _parse_markup_rules_from_form(form):
    mins = form.getlist("markup_range_min[]")
    maxes = form.getlist("markup_range_max[]")
    percents = form.getlist("markup_percent[]")

    length = max(len(mins), len(maxes), len(percents))
    parsed_rows = []
    rules = []
    row_errors = {}

    for index in range(length):
        min_text = str(mins[index] if index < len(mins) else "").strip()
        max_text = str(maxes[index] if index < len(maxes) else "").strip()
        percent_text = str(percents[index] if index < len(percents) else "").strip()

        if not min_text and not max_text and not percent_text:
            continue

        parsed_rows.append(
            {
                "range_min": min_text,
                "range_max": max_text,
                "markup_percent": percent_text,
            }
        )

        errors = []
        try:
            range_min = float(min_text)
        except ValueError:
            errors.append("Range min must be a valid number.")
            range_min = None

        range_max = None
        if max_text:
            try:
                range_max = float(max_text)
            except ValueError:
                errors.append("Range max must be a valid number.")

        try:
            markup_percent = float(percent_text)
        except ValueError:
            errors.append("Markup percent must be a valid number.")
            markup_percent = None

        if range_min is not None and range_min < 0:
            errors.append("Range min must be zero or greater.")

        if range_min is not None and range_max is not None and range_min >= range_max:
            errors.append("Range min must be less than range max.")

        if markup_percent is not None and markup_percent <= 0:
            errors.append("Markup percent must be greater than zero.")

        if errors:
            row_errors[len(parsed_rows) - 1] = errors
            continue

        rules.append(
            {
                "row_index": len(parsed_rows) - 1,
                "range_min": range_min,
                "range_max": range_max,
                "markup_percent": markup_percent,
            }
        )

    rules.sort(key=lambda rule: rule["range_min"])

    none_max_count = sum(1 for rule in rules if rule["range_max"] is None)
    if none_max_count > 1:
        for rule in rules:
            if rule["range_max"] is None:
                row_errors.setdefault(rule["row_index"], []).append("Only one rule can have no upper limit.")

    for index, rule in enumerate(rules):
        if rule["range_max"] is None and index != len(rules) - 1:
            row_errors.setdefault(rule["row_index"], []).append("No-limit rule must be the highest range.")

    for index in range(1, len(rules)):
        previous = rules[index - 1]
        current = rules[index]
        previous_max = previous["range_max"]
        if previous_max is None or current["range_min"] < previous_max:
            row_errors.setdefault(previous["row_index"], []).append("Range overlaps with another rule.")
            row_errors.setdefault(current["row_index"], []).append("Range overlaps with another rule.")

    cleaned_rules = [
        {
            "range_min": rule["range_min"],
            "range_max": rule["range_max"],
            "markup_percent": rule["markup_percent"],
        }
        for rule in rules
    ]

    return cleaned_rules, parsed_rows, row_errors


def _canonicalize_markup_rules(rules):
    canonical_rows = []
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        try:
            range_min = round(float(rule.get("range_min") or 0.0), 4)
            markup_percent = round(float(rule.get("markup_percent") or 0.0), 4)
        except (TypeError, ValueError):
            continue

        range_max_raw = rule.get("range_max")
        range_max = None
        if range_max_raw not in [None, ""]:
            try:
                range_max = round(float(range_max_raw), 4)
            except (TypeError, ValueError):
                range_max = None

        canonical_rows.append((range_min, range_max, markup_percent))

    canonical_rows.sort(key=lambda row: (row[0], float("inf") if row[1] is None else row[1], row[2]))
    return canonical_rows


def _markup_status_payload(status):
    if status == "saved":
        return "success", "Markup rules saved. These rules apply to new parts and materials going forward. Existing sell prices are unchanged."
    if status == "recalculated":
        return "success", "Existing auto-populated sell prices were recalculated using your latest markup rules."
    if status == "kept":
        return "success", "Existing sell prices were kept unchanged."
    if status == "recalc_failed":
        return "error", "Unable to recalculate prices because markup rules are missing or invalid."
    return "", ""


def _parse_tax_rates_from_form(form):
    names = form.getlist("tax_rate_name[]")
    rates = form.getlist("tax_rate_rate[]")
    agencies = form.getlist("tax_rate_agency[]")
    applies_to_parts_values = form.getlist("tax_rate_applies_to_parts[]")
    applies_to_materials_values = form.getlist("tax_rate_applies_to_materials[]")
    applies_to_equipment_values = form.getlist("tax_rate_applies_to_equipment[]")
    applies_to_services_values = form.getlist("tax_rate_applies_to_services[]")
    applies_to_labor_values = form.getlist("tax_rate_applies_to_labor[]")
    applies_to_parts_set = set(str(value).strip() for value in applies_to_parts_values)
    applies_to_materials_set = set(str(value).strip() for value in applies_to_materials_values)
    applies_to_equipment_set = set(str(value).strip() for value in applies_to_equipment_values)
    applies_to_services_set = set(str(value).strip() for value in applies_to_services_values)
    applies_to_labor_set = set(str(value).strip() for value in applies_to_labor_values)
    active_values = form.getlist("tax_rate_active[]")
    quickbooks_codes = form.getlist("tax_rate_quickbooks_tax_code[]")

    length = max(
        len(names),
        len(rates),
        len(agencies),
        len(active_values),
        len(quickbooks_codes),
    )

    parsed = []
    for index in range(length):
        name = str(names[index] if index < len(names) else "").strip()
        if not name:
            continue

        try:
            rate = float(str(rates[index] if index < len(rates) else "0").strip() or "0")
        except ValueError:
            rate = 0.0

        applies_to = []
        row_key = str(index)
        if row_key in applies_to_parts_set:
            applies_to.append("parts")
        if row_key in applies_to_materials_set:
            applies_to.append("materials")
        if row_key in applies_to_equipment_set:
            applies_to.append("equipment")
        if row_key in applies_to_services_set:
            applies_to.append("services")
        if row_key in applies_to_labor_set:
            applies_to.append("labor")

        if not applies_to:
            applies_to = list(DEFAULT_TAX_RATE_TYPES)

        active = _is_truthy_form_value(active_values[index] if index < len(active_values) else "true")

        display_order = index

        parsed.append(
            {
                "name": name,
                "rate": rate,
                "applies_to": applies_to,
                "agency": str(agencies[index] if index < len(agencies) else "").strip(),
                "active": active,
                "display_order": display_order,
                "quickbooks_tax_code": str(quickbooks_codes[index] if index < len(quickbooks_codes) else "").strip(),
            }
        )

    return sorted(parsed, key=lambda row: row.get("display_order", 0))


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
    business["tax_rates"] = _normalize_tax_rates_for_view(business)
    business["markup_rules"] = _normalize_markup_rules_for_view(business)
    business["payment_schedule_templates"] = _normalize_payment_schedule_templates_for_view(business)

    custom_logo = str(business.get("custom_logo") or "").strip()
    logo_url = ""
    if custom_logo:
        logo_url = url_for("business.view_logo")
    logo_version = str(business.get("custom_logo_uploaded_at") or "").strip()
    if logo_url and logo_version:
        separator = "&" if "?" in logo_url else "?"
        logo_url = f"{logo_url}{separator}v={quote(logo_version, safe='')}"

    logo_status_kind, logo_status_message = _logo_status_payload(request.args.get("logo_status", ""))
    stripe_status_kind, stripe_status_message = _stripe_status_payload(request.args.get("stripe_status", ""))
    twilio_status_kind, twilio_status_message = _twilio_status_payload(business)
    markup_status_kind, markup_status_message = _markup_status_payload(request.args.get("markup_status", ""))
    show_markup_recalc_prompt = str(request.args.get("show_markup_recalc_prompt") or "").strip() == "1"

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
        twilio_status_kind=twilio_status_kind,
        twilio_status_message=twilio_status_message,
        markup_status_kind=markup_status_kind,
        markup_status_message=markup_status_message,
        show_markup_recalc_prompt=show_markup_recalc_prompt,
    )


@bp.route("/business/logo/view")
def view_logo():
    if not _is_authorized():
        return Response(status=403)

    db = ensure_connection_or_500()
    employee, _, business = _business_context(db)
    if not employee:
        session.clear()
        return Response(status=401)
    if not business:
        return Response(status=404)

    custom_logo = str((business or {}).get("custom_logo") or "").strip()
    if not custom_logo:
        return Response(status=404)

    logo_bytes = object_storage.download_object_bytes(custom_logo)
    if not logo_bytes:
        fallback_url = object_storage.build_access_url(custom_logo)
        if fallback_url:
            return redirect(fallback_url)
        return Response(status=404)

    extension = custom_logo.rsplit(".", 1)[-1].lower() if "." in custom_logo else ""
    mime_type = _LOGO_MIME_TYPES.get(extension, "image/png")

    return Response(
        logo_bytes,
        mimetype=mime_type,
        headers={
            "Cache-Control": "private, max-age=300",
        },
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

            save_image = image_file.convert("RGBA")
            save_image.thumbnail((1200, 600), Image.Resampling.LANCZOS)

            encoded = BytesIO()
            save_image.save(encoded, format="PNG", optimize=True)
            object_key = f"logos/{str(business_oid)}_logo.png"
            object_storage.upload_bytes(
                object_key=object_key,
                data=encoded.getvalue(),
                content_type="image/png",
            )

    except Exception:
        return redirect(url_for("business.business_profile", logo_status="upload_failed"))

    db.businesses.update_one(
        {"_id": business_oid},
        {
            "$set": {
                "custom_logo": object_key,
                "custom_logo_uploaded_at": datetime.now(UTC),
            }
        },
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
        existing_markup_rules = _canonicalize_markup_rules((business or {}).get("markup_rules") or [])
        company_name = request.form.get("company_name", "").strip()
        warranty_info = request.form.get("warranty_info", "").strip()
        address_line_1 = request.form.get("address_line_1", "").strip()
        address_line_2 = request.form.get("address_line_2", "").strip()
        city = request.form.get("city", "").strip()
        state = request.form.get("state", "").strip().upper()
        zip_code = request.form.get("zip_code", "").strip()
        phone_number = request.form.get("phone_number", "").strip()
        twilio_phone_number = request.form.get("twilio_phone_number", "").strip()
        fax_number = request.form.get("fax_number", "").strip()
        email = request.form.get("email", "").strip()
        website = request.form.get("website", "").strip()
        license_number = request.form.get("license_number", "").strip()
        tax_rates = _parse_tax_rates_from_form(request.form)
        quote_email_template = request.form.get("quote_email_template", "").strip()
        invoice_email_template = request.form.get("invoice_email_template", "").strip()
        report_email_template = request.form.get("report_email_template", "").strip()
        payment_schedule_templates, payment_schedule_templates_rows, payment_schedule_templates_errors = _parse_payment_schedule_templates_from_form(request.form)
        default_estimate_expiration_days_raw = request.form.get("default_estimate_expiration_days", "").strip()
        default_payment_due_days_raw = request.form.get("default_payment_due_days", "").strip()
        qb_material_account_id = request.form.get("qb_material_account_id", "").strip()
        labor_rate_standard = _parse_optional_nonnegative_float(request.form.get("labor_rate_standard", ""))
        labor_rate_emergency = _parse_optional_nonnegative_float(request.form.get("labor_rate_emergency", ""))
        markup_rules, markup_rules_rows, markup_rules_errors = _parse_markup_rules_from_form(request.form)

        try:
            default_estimate_expiration_days = max(1, int(default_estimate_expiration_days_raw))
        except ValueError:
            default_estimate_expiration_days = 30

        try:
            default_payment_due_days = max(1, int(default_payment_due_days_raw))
        except ValueError:
            default_payment_due_days = 30

        if not markup_rules_errors and not payment_schedule_templates_errors:
            markup_rules_changed = existing_markup_rules != _canonicalize_markup_rules(markup_rules)
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
                        "twilio_phone_number": twilio_phone_number,
                        "fax_number": fax_number,
                        "email": email,
                        "website": website,
                        "license_number": license_number,
                        "tax_rates": tax_rates,
                        "quote_email_template": quote_email_template,
                        "invoice_email_template": invoice_email_template,
                        "report_email_template": report_email_template,
                        "payment_schedule_templates": payment_schedule_templates,
                        "default_estimate_expiration_days": default_estimate_expiration_days,
                        "default_payment_due_days": default_payment_due_days,
                        "qb_material_account_id": qb_material_account_id,
                        "labor_rate_standard": labor_rate_standard,
                        "labor_rate_emergency": labor_rate_emergency,
                        "markup_rules": markup_rules,
                    }
                },
            )

            redirect_kwargs = {"markup_status": "saved"}
            if markup_rules_changed:
                redirect_kwargs["show_markup_recalc_prompt"] = "1"
            return redirect(url_for("business.business_profile", **redirect_kwargs))

        business = serialize_doc(db.businesses.find_one({"_id": business_oid}) or {})
        business["tax_rates"] = tax_rates
        business["company_name"] = company_name
        business["warranty_info"] = warranty_info
        business["address_line_1"] = address_line_1
        business["address_line_2"] = address_line_2
        business["city"] = city
        business["state"] = state
        business["zip_code"] = zip_code
        business["phone_number"] = phone_number
        business["twilio_phone_number"] = twilio_phone_number
        business["fax_number"] = fax_number
        business["email"] = email
        business["website"] = website
        business["license_number"] = license_number
        business["quote_email_template"] = quote_email_template
        business["invoice_email_template"] = invoice_email_template
        business["report_email_template"] = report_email_template
        business["payment_schedule_templates"] = payment_schedule_templates_rows
        business["default_estimate_expiration_days"] = default_estimate_expiration_days
        business["default_payment_due_days"] = default_payment_due_days
        business["qb_material_account_id"] = qb_material_account_id
        business["labor_rate_standard"] = "" if labor_rate_standard is None else f"{labor_rate_standard:.2f}"
        business["labor_rate_emergency"] = "" if labor_rate_emergency is None else f"{labor_rate_emergency:.2f}"
        business["markup_rules"] = markup_rules_rows
        return render_template(
            "business/update_business.html",
            business=business,
            markup_rules_errors=markup_rules_errors,
            markup_rules_error_message="Fix the highlighted errors before saving.",
            payment_schedule_templates_errors=payment_schedule_templates_errors,
            payment_schedule_templates_error_message="Fix installation payment schedule errors before saving.",
            installation_payment_stage_options=INSTALLATION_PAYMENT_STAGE_OPTIONS,
            installation_trigger_label_map=INSTALLATION_TRIGGER_LABEL_MAP,
        )

    business = db.businesses.find_one({"_id": business_oid})
    if not business:
        return redirect(url_for("error_page", error="no_business"))

    business = serialize_doc(business)
    business["tax_rates"] = _normalize_tax_rates_for_view(business)
    business["address_line_1"] = str(business.get("address_line_1") or "").strip()
    business["address_line_2"] = str(business.get("address_line_2") or "").strip()
    business["city"] = str(business.get("city") or "").strip()
    business["state"] = str(business.get("state") or "").strip().upper()
    business["zip_code"] = str(business.get("zip_code") or "").strip()
    business["phone_number"] = str(business.get("phone_number") or "").strip()
    business["twilio_phone_number"] = str(business.get("twilio_phone_number") or "").strip()
    business["fax_number"] = str(business.get("fax_number") or "").strip()
    business["email"] = str(business.get("email") or "").strip()
    business["website"] = str(business.get("website") or "").strip()
    business["license_number"] = str(business.get("license_number") or "").strip()
    business["warranty_info"] = str(business.get("warranty_info") or "").strip()
    business["report_email_template"] = str(business.get("report_email_template") or "").strip()
    business["payment_schedule_templates"] = _normalize_payment_schedule_templates_for_view(business)
    business["qb_material_account_id"] = str(business.get("qb_material_account_id") or "").strip()
    try:
        business["default_estimate_expiration_days"] = max(1, int(business.get("default_estimate_expiration_days") or 30))
    except (TypeError, ValueError):
        business["default_estimate_expiration_days"] = 30
    try:
        business["default_payment_due_days"] = max(1, int(business.get("default_payment_due_days") or 30))
    except (TypeError, ValueError):
        business["default_payment_due_days"] = 30
    labor_rate_standard = _parse_optional_nonnegative_float(business.get("labor_rate_standard"))
    labor_rate_emergency = _parse_optional_nonnegative_float(business.get("labor_rate_emergency"))
    business["labor_rate_standard"] = "" if labor_rate_standard is None else f"{labor_rate_standard:.2f}"
    business["labor_rate_emergency"] = "" if labor_rate_emergency is None else f"{labor_rate_emergency:.2f}"
    business["markup_rules"] = _normalize_markup_rules_for_view(business)
    return render_template(
        "business/update_business.html",
        business=business,
        markup_rules_errors={},
        markup_rules_error_message="",
        payment_schedule_templates_errors={},
        payment_schedule_templates_error_message="",
        installation_payment_stage_options=INSTALLATION_PAYMENT_STAGE_OPTIONS,
        installation_trigger_label_map=INSTALLATION_TRIGGER_LABEL_MAP,
    )


@bp.route("/business/markup/recalculate", methods=["POST"])
def recalculate_markup_prices():
    if not _is_authorized():
        return redirect(url_for("admin_bp.admin"))

    db = ensure_connection_or_500()
    employee, business_oid, business = _business_context(db)
    if not employee:
        session.clear()
        return redirect(url_for("auth.login"))
    if not business_oid or not business:
        return redirect(url_for("error_page", error="no_business"))

    markup_rules = (business or {}).get("markup_rules")
    if not isinstance(markup_rules, list) or not markup_rules:
        return redirect(url_for("business.business_profile", markup_status="recalc_failed"))

    part_updates = 0
    for part in db.parts.find({"business_id": business_oid, "sell_price_auto_populated": True}):
        cost_price = part.get("cost_price")
        rule = get_markup_rule(cost_price, markup_rules)
        if not rule:
            continue
        sell_price = calculate_sell_price(cost_price, rule.get("markup_percent"))
        result = db.parts.update_one(
            {"_id": part.get("_id")},
            {
                "$set": {
                    "sell_price": sell_price,
                    "price": sell_price,
                    "sell_price_auto_populated": True,
                }
            },
        )
        if result.modified_count:
            part_updates += 1

    material_updates = 0
    for material in db.materials.find({"business_id": business_oid, "sell_price_auto_populated": True}):
        cost_price_per_unit = material.get("cost_price_per_unit")
        rule = get_markup_rule(cost_price_per_unit, markup_rules)
        if not rule:
            continue
        sell_price_per_unit = calculate_sell_price(cost_price_per_unit, rule.get("markup_percent"))
        result = db.materials.update_one(
            {"_id": material.get("_id")},
            {
                "$set": {
                    "sell_price_per_unit": sell_price_per_unit,
                    "price": sell_price_per_unit,
                    "sell_price_auto_populated": True,
                }
            },
        )
        if result.modified_count:
            material_updates += 1

    return redirect(
        url_for(
            "business.business_profile",
            markup_status="recalculated",
            updated_parts=part_updates,
            updated_materials=material_updates,
        )
    )
