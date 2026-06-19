import base64
import os
import re


def sms_features_enabled():
    raw_value = str(os.getenv("SMS_FEATURES_ENABLED", "true") or "").strip().lower()
    return raw_value not in {"0", "false", "no", "off"}


def get_email_default_sender():
    return str(
        os.getenv("SENDGRID_DEFAULT_SENDER")
        or os.getenv("MAIL_DEFAULT_SENDER")
        or ""
    ).strip()


def get_email_default_sender_name():
    return str(
        os.getenv("SENDGRID_DEFAULT_SENDER_NAME")
        or os.getenv("MAIL_DEFAULT_SENDER_NAME")
        or ""
    ).strip()


def resolve_business_email_identity(business):
    """Derive the From/Reply-To identity for emails sent on behalf of a business.

    Returns a (from_email, from_name, reply_to) tuple.

    The From address is always the verified SendGrid sender (SENDGRID_DEFAULT_SENDER)
    because SendGrid rejects any From address that is not a verified single sender
    or part of an authenticated domain (HTTP 403). The business name is used as the
    display name and the business email is used as Reply-To so customer replies are
    routed back to the business.
    """
    business = business or {}
    business_name = str(
        business.get("company_name")
        or business.get("business_name")
        or business.get("name")
        or ""
    ).strip()
    business_email = str(business.get("email") or "").strip()

    from_email = get_email_default_sender()
    from_name = business_name or get_email_default_sender_name()
    reply_to = business_email or None
    return from_email, from_name, reply_to



def _email_ssl_verify_disabled():
    raw_value = str(os.getenv("SENDGRID_DISABLE_SSL_VERIFY") or "").strip().lower()
    return raw_value in {"1", "true", "yes", "on"}


def _apply_email_ssl_context():
    """Make the SendGrid SDK's urllib calls use a trusted CA bundle.

    The SendGrid SDK sends through urllib, which on some machines (e.g. behind
    antivirus/corporate SSL inspection) cannot verify the certificate chain.
    Use certifi's bundle when available, and allow disabling verification for
    local development via SENDGRID_DISABLE_SSL_VERIFY.
    """
    import ssl

    if _email_ssl_verify_disabled():
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        ssl._create_default_https_context = lambda *args, **kwargs: context
        return

    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
        ssl._create_default_https_context = lambda *args, **kwargs: context
    except Exception:
        # Fall back to the interpreter default trust store.
        pass


def send_email(
    subject,
    recipients,
    body,
    attachments=None,
    from_email=None,
    from_name=None,
    reply_to=None,
    business=None,
):
    """Send a plain-text email through SendGrid.

    recipients: a single email string or an iterable of email strings.
    attachments: optional iterable of (filename, mimetype, content_bytes) tuples.
    from_name: display name shown in the recipient's inbox (e.g. the business name).
    reply_to: address replies should be directed to.
    business: optional business settings doc; when provided its name/email are
        used to populate the From display name, sender, and reply-to.
    Raises RuntimeError if the message cannot be sent so callers can surface
    the failure the same way they did with Flask-Mail.
    """
    api_key = str(os.getenv("SENDGRID_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("SENDGRID_API_KEY is not configured")

    if business is not None:
        biz_from_email, biz_from_name, biz_reply_to = resolve_business_email_identity(business)
        from_email = from_email or biz_from_email
        from_name = from_name or biz_from_name
        reply_to = reply_to or biz_reply_to

    sender = str(from_email or get_email_default_sender() or "").strip()
    if not sender:
        raise RuntimeError("Email sender address is not configured")

    sender_name = str(from_name or get_email_default_sender_name() or "").strip()
    reply_to_value = str(reply_to or "").strip()

    if isinstance(recipients, str):
        recipient_list = [recipients.strip()] if recipients.strip() else []
    else:
        recipient_list = [str(r).strip() for r in (recipients or []) if str(r).strip()]
    if not recipient_list:
        raise RuntimeError("No email recipients provided")

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import (
            Attachment,
            ClickTracking,
            Disposition,
            Email,
            FileContent,
            FileName,
            FileType,
            Mail,
            ReplyTo,
            TrackingSettings,
        )
    except Exception as exc:
        raise RuntimeError(f"SendGrid SDK is not installed: {exc}")

    message = Mail(
        from_email=Email(sender, sender_name) if sender_name else Email(sender),
        to_emails=recipient_list,
        subject=subject,
        plain_text_content=body or "",
    )

    # Disable SendGrid click tracking so recipients see the real estimate/invoice
    # links instead of long, spam-looking sendgrid.net redirect URLs.
    tracking_settings = TrackingSettings()
    tracking_settings.click_tracking = ClickTracking(False, False)
    message.tracking_settings = tracking_settings

    if reply_to_value:
        message.reply_to = ReplyTo(reply_to_value)

    for attachment in attachments or []:
        try:
            filename, mimetype, content_bytes = attachment
        except (TypeError, ValueError):
            continue
        encoded_content = base64.b64encode(content_bytes).decode("ascii")
        message.add_attachment(
            Attachment(
                FileContent(encoded_content),
                FileName(filename),
                FileType(mimetype or "application/octet-stream"),
                Disposition("attachment"),
            )
        )

    try:
        _apply_email_ssl_context()
        client = SendGridAPIClient(api_key)
        response = client.send(message)
    except Exception as exc:
        raise RuntimeError(f"SendGrid send failed: {exc}")

    status_code = getattr(response, "status_code", 0) or 0
    if status_code < 200 or status_code >= 300:
        raise RuntimeError(f"SendGrid returned status {status_code}")



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