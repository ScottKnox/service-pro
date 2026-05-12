import os
import re


def sms_features_enabled():
    raw_value = str(os.getenv("SMS_FEATURES_ENABLED", "true") or "").strip().lower()
    return raw_value not in {"0", "false", "no", "off"}


def normalize_phone_for_twilio(value, default_country_code="+1"):
    raw = str(value or "").strip()
    if not raw:
        return ""

    keep_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""

    if keep_plus:
        return f"+{digits}"

    if len(digits) == 10:
        return f"{default_country_code}{digits}"

    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"

    if default_country_code.startswith("+"):
        return f"{default_country_code}{digits}"

    return f"+{digits}"


def send_sms_via_twilio(to_number, from_number, message_body, status_callback_url=""):
    account_sid = str(os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    auth_token = str(os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    if not account_sid or not auth_token:
        return False, "Twilio credentials are not configured"

    if not to_number or not from_number or not message_body:
        return False, "Missing SMS payload fields"

    try:
        from twilio.rest import Client
    except Exception:
        return False, "Twilio SDK is not installed"

    try:
        client = Client(account_sid, auth_token)
        message_kwargs = {
            "body": message_body,
            "from_": from_number,
            "to": to_number,
        }
        if status_callback_url:
            message_kwargs["status_callback"] = status_callback_url

        msg = client.messages.create(**message_kwargs)
        message_sid = str(getattr(msg, "sid", "") or "").strip()
        message_status = str(getattr(msg, "status", "") or "").strip()
        return True, {"sid": message_sid, "status": message_status}
    except Exception as exc:
        return False, str(exc)