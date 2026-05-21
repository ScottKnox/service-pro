import os
from functools import lru_cache
from urllib.parse import quote, urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError


def _get_setting(name):
    return str(os.getenv(name) or "").strip()


def _config():
    return {
        "endpoint_url": _get_setting("SPACES_ENDPOINT_URL"),
        "region": _get_setting("SPACES_REGION"),
        "bucket": _get_setting("SPACES_BUCKET"),
        "access_key": _get_setting("SPACES_ACCESS_KEY_ID"),
        "secret_key": _get_setting("SPACES_SECRET_ACCESS_KEY"),
        "key_prefix": _get_setting("SPACES_KEY_PREFIX"),
        "cdn_base_url": _get_setting("SPACES_CDN_BASE_URL"),
        "use_signed_urls": _get_setting("SPACES_USE_SIGNED_URLS"),
        "signed_url_ttl_seconds": _get_setting("SPACES_SIGNED_URL_TTL_SECONDS"),
    }


def is_configured():
    cfg = _config()
    required = [
        cfg["endpoint_url"],
        cfg["region"],
        cfg["bucket"],
        cfg["access_key"],
        cfg["secret_key"],
    ]
    return all(required)


@lru_cache(maxsize=1)
def _client():
    cfg = _config()
    if not is_configured():
        raise RuntimeError("Object storage is not configured.")

    return boto3.client(
        "s3",
        region_name=cfg["region"],
        endpoint_url=cfg["endpoint_url"],
        aws_access_key_id=cfg["access_key"],
        aws_secret_access_key=cfg["secret_key"],
    )


def _normalize_key(value):
    key = str(value or "").strip().lstrip("/")
    if not key:
        return ""

    prefix = str(_config().get("key_prefix") or "").strip().strip("/")
    if prefix and not key.startswith(f"{prefix}/"):
        key = f"{prefix}/{key}"
    return key


def build_public_url(object_key):
    raw = str(object_key or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw

    key = _normalize_key(raw)
    if not key:
        return ""
    encoded_key = quote(key, safe="/")

    cfg = _config()
    cdn_base_url = str(cfg.get("cdn_base_url") or "").rstrip("/")
    if cdn_base_url:
        return f"{cdn_base_url}/{encoded_key}"

    endpoint = str(cfg.get("endpoint_url") or "").rstrip("/")
    bucket = str(_config().get("bucket") or "").strip()
    if not endpoint or not bucket:
        return ""

    parsed_endpoint = urlparse(endpoint)
    endpoint_host = str(parsed_endpoint.netloc or "").strip().lower()
    bucket_prefix = f"{bucket.lower()}."
    endpoint_path = str(parsed_endpoint.path or "").strip("/")

    # Support both endpoint styles:
    # 1) Region endpoint: https://nyc3.digitaloceanspaces.com -> /<bucket>/<key>
    # 2) Bucket endpoint: https://<bucket>.nyc3.digitaloceanspaces.com -> /<key>
    if endpoint_host.startswith(bucket_prefix) or endpoint_path == bucket:
        return f"{endpoint}/{encoded_key}"

    return f"{endpoint}/{bucket}/{encoded_key}"


def _extract_key(object_key_or_url):
    raw = str(object_key_or_url or "").strip()
    if not raw:
        return ""

    if not (raw.startswith("http://") or raw.startswith("https://")):
        return _normalize_key(raw)

    parsed = urlparse(raw)
    path = parsed.path.lstrip("/")
    if not path:
        return ""

    bucket = str(_config().get("bucket") or "").strip()
    endpoint_host = urlparse(str(_config().get("endpoint_url") or "")).netloc

    if bucket and path.startswith(f"{bucket}/"):
        return _normalize_key(path[len(bucket) + 1 :])

    host = parsed.netloc
    if bucket and host.startswith(f"{bucket}."):
        return _normalize_key(path)

    if endpoint_host and host == endpoint_host and bucket and path.startswith(f"{bucket}/"):
        return _normalize_key(path[len(bucket) + 1 :])

    return _normalize_key(path)


def _use_signed_urls():
    return str(_config().get("use_signed_urls") or "").strip().lower() in {"1", "true", "yes", "on"}


def _signed_url_ttl_seconds():
    raw = str(_config().get("signed_url_ttl_seconds") or "").strip()
    try:
        ttl = int(raw)
    except ValueError:
        ttl = 900
    return max(60, ttl)


def build_access_url(object_key_or_url):
    raw = str(object_key_or_url or "").strip()
    if not raw:
        return ""

    if not is_configured():
        return raw if raw.startswith("http://") or raw.startswith("https://") else ""

    key = _extract_key(raw)
    if not key:
        return raw if raw.startswith("http://") or raw.startswith("https://") else ""

    if _use_signed_urls():
        cfg = _config()
        try:
            return _client().generate_presigned_url(
                "get_object",
                Params={"Bucket": cfg["bucket"], "Key": key},
                ExpiresIn=_signed_url_ttl_seconds(),
            )
        except (BotoCoreError, ClientError):
            return ""

    return build_public_url(key)


def upload_bytes(object_key, data, content_type="application/octet-stream"):
    key = _normalize_key(object_key)
    if not key:
        raise ValueError("Object key is required.")

    if not is_configured():
        raise RuntimeError("Object storage is not configured.")

    body = data if isinstance(data, (bytes, bytearray)) else bytes(data or b"")
    cfg = _config()

    _client().put_object(
        Bucket=cfg["bucket"],
        Key=key,
        Body=body,
        ContentType=str(content_type or "application/octet-stream"),
        ACL="public-read",
    )
    return build_public_url(key)


def upload_file_stream(object_key, stream, content_type="application/octet-stream"):
    if stream is None:
        raise ValueError("File stream is required.")
    stream.seek(0)
    data = stream.read()
    stream.seek(0)
    return upload_bytes(object_key=object_key, data=data, content_type=content_type)


def delete_object(object_key_or_url):
    key = _extract_key(object_key_or_url)
    if not key or not is_configured():
        return False

    cfg = _config()
    try:
        _client().delete_object(Bucket=cfg["bucket"], Key=key)
        return True
    except (BotoCoreError, ClientError):
        return False


def download_object_bytes(object_key_or_url):
    key = _extract_key(object_key_or_url)
    if not key or not is_configured():
        return b""

    cfg = _config()
    try:
        response = _client().get_object(Bucket=cfg["bucket"], Key=key)
        body = response.get("Body")
        return body.read() if body else b""
    except (BotoCoreError, ClientError):
        return b""
