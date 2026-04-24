import os
import logging
from datetime import date, datetime
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from bson import ObjectId
from dotenv import load_dotenv
from flask import abort
from pymongo import MongoClient
from pymongo.errors import PyMongoError, OperationFailure

load_dotenv()

logger = logging.getLogger(__name__)

MONGODB_HOST = os.getenv("MONGODB_HOST", "localhost")
MONGODB_PORT = os.getenv("MONGODB_PORT", "27017")
MONGODB_USERNAME = os.getenv("MONGODB_USERNAME", "")
MONGODB_PASSWORD = os.getenv("MONGODB_PASSWORD", "")
MONGODB_AUTH_SOURCE = os.getenv("MONGODB_AUTH_SOURCE", "admin")
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME")

_mongo_client = None
_validators_initialized = False


def _inject_credentials_into_uri(uri: str) -> str:
    if not (MONGODB_USERNAME and MONGODB_PASSWORD):
        return uri

    parts = urlsplit(uri)
    if "@" in parts.netloc:
        return uri

    username = quote_plus(MONGODB_USERNAME)
    password = quote_plus(MONGODB_PASSWORD)
    netloc = f"{username}:{password}@{parts.netloc}"

    query = parts.query
    if MONGODB_AUTH_SOURCE and parts.scheme == "mongodb":
        query_items = dict(parse_qsl(query, keep_blank_values=True))
        query_items.setdefault("authSource", MONGODB_AUTH_SOURCE)
        query = urlencode(query_items)

    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def build_mongodb_uri() -> str:
    if MONGODB_URI:
        return _inject_credentials_into_uri(MONGODB_URI)

    if MONGODB_HOST.startswith("mongodb://") or MONGODB_HOST.startswith("mongodb+srv://"):
        return _inject_credentials_into_uri(MONGODB_HOST)

    credentials = ""
    if MONGODB_USERNAME and MONGODB_PASSWORD:
        username = quote_plus(MONGODB_USERNAME)
        password = quote_plus(MONGODB_PASSWORD)
        credentials = f"{username}:{password}@"

    uri = f"mongodb://{credentials}{MONGODB_HOST}:{MONGODB_PORT}"
    if credentials:
        uri = f"{uri}/?authSource={quote_plus(MONGODB_AUTH_SOURCE)}"
    return uri


def get_db():
    global _mongo_client
    global _validators_initialized
    if _mongo_client is None:
        _mongo_client = MongoClient(build_mongodb_uri(), serverSelectionTimeoutMS=3000)
    db = _mongo_client[MONGODB_DB_NAME]
    if not _validators_initialized:
        ensure_collection_validators(db)
        _validators_initialized = True
    return db


def object_id_or_404(value: str) -> ObjectId:
    if not ObjectId.is_valid(value):
        abort(404)
    return ObjectId(value)


def serialize_doc(doc):
    if not doc:
        return None

    def _serialize_value(value):
        if isinstance(value, ObjectId):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, list):
            return [_serialize_value(item) for item in value]
        if isinstance(value, dict):
            return {key: _serialize_value(val) for key, val in value.items()}
        return value

    return {key: _serialize_value(value) for key, value in doc.items()}


def ensure_connection_or_500():
    try:
        db = get_db()
        db.command("ping")
        return db
    except PyMongoError as exc:
        logger.error("MongoDB connection failed: %s", exc)
        abort(500, description=f"MongoDB connection failed: {exc}")


def coerce_object_id(value):
    if isinstance(value, ObjectId):
        return value
    text = str(value or "").strip()
    if not text or not ObjectId.is_valid(text):
        return None
    return ObjectId(text)


def build_reference_filter(field_name, value):
    oid_value = coerce_object_id(value)
    text_value = str(value or "").strip()

    predicates = []
    if oid_value is not None:
        predicates.append({field_name: oid_value})
        predicates.append({field_name: str(oid_value)})
    elif text_value:
        predicates.append({field_name: text_value})

    if not predicates:
        return {field_name: ""}
    if len(predicates) == 1:
        return predicates[0]
    return {"$or": predicates}


def reference_value(value):
    oid_value = coerce_object_id(value)
    return oid_value if oid_value is not None else str(value or "").strip()


def _ensure_collection_with_validator(db, collection_name, validator):
    existing_names = db.list_collection_names()
    if collection_name not in existing_names:
        db.create_collection(collection_name, validator=validator)
        return

    try:
        db.command(
            {
                "collMod": collection_name,
                "validator": validator,
                "validationLevel": "moderate",
                "validationAction": "warn",
            }
        )
    except OperationFailure:
        logger.warning("Unable to apply validator for collection %s", collection_name)


def ensure_collection_validators(db):
    jobs_validator = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["customer_id", "status", "services", "total", "total_amount"],
            "properties": {
                "customer_id": {"bsonType": ["objectId"]},
                "business_id": {"bsonType": ["objectId", "null"]},
                "status": {"enum": ["Pending", "Scheduled", "Started", "Completed", "Paid"]},
                "services": {"bsonType": "array"},
                "parts": {"bsonType": "array"},
                "materials": {"bsonType": "array"},
                "labors": {"bsonType": "array"},
                "equipments": {"bsonType": "array"},
                "discounts": {"bsonType": "array"},
                "total": {"bsonType": ["string", "null"]},
                "total_amount": {"bsonType": ["double", "int", "long", "decimal"]},
                "created_at": {"bsonType": ["date"]},
                "scheduled_at": {"bsonType": ["date", "null"]},
                "completed_at": {"bsonType": ["date", "null"]},
            },
        }
    }

    estimates_validator = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["customer_id", "status", "services", "total", "total_amount"],
            "properties": {
                "customer_id": {"bsonType": ["objectId"]},
                "status": {"enum": ["Created", "Sent", "Accepted", "Declined"]},
                "services": {"bsonType": "array"},
                "parts": {"bsonType": "array"},
                "materials": {"bsonType": "array"},
                "labors": {"bsonType": "array"},
                "equipments": {"bsonType": "array"},
                "discounts": {"bsonType": "array"},
                "total": {"bsonType": ["string", "null"]},
                "total_amount": {"bsonType": ["double", "int", "long", "decimal"]},
                "created_at": {"bsonType": ["date"]},
            },
        }
    }

    services_validator = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["business_id", "service_name", "service_type", "service_code", "standard_price", "emergency", "emergency_price"],
            "properties": {
                "business_id": {"bsonType": ["objectId"]},
                "service_name": {"bsonType": "string"},
                "service_type": {"bsonType": "string"},
                "service_code": {"bsonType": "string"},
                "standard_price": {"bsonType": ["double", "int", "long", "decimal"]},
                "emergency": {"bsonType": ["bool"]},
                "emergency_price": {"bsonType": ["double", "int", "long", "decimal"]},
                "materials_cost": {"bsonType": ["double", "int", "long", "decimal"]},
                "estimated_hours": {"bsonType": ["double", "int", "long", "decimal"]},
                "part_ids": {"bsonType": "array"},
                "material_ids": {"bsonType": "array"},
                "service_parts": {"bsonType": "array"},
                "service_materials": {"bsonType": "array"},
            },
        }
    }

    hvac_systems_validator = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["customer_id", "system_type", "property_id"],
            "properties": {
                "customer_id": {"bsonType": ["objectId"]},
                "system_type": {"bsonType": "string"},
                "property_id": {"bsonType": ["objectId", "string"]},
                "system_tonnage": {"bsonType": ["string", "null"]},
                "cooling_capacity": {"bsonType": ["string", "null"]},
                "heating_capacity": {"bsonType": ["string", "null"]},
                "components": {"bsonType": ["object", "null"]},
                "diagnostics": {"bsonType": ["array", "object"]},
                "photos": {"bsonType": ["array", "null"]},
            },
        }
    }

    subscriptions_validator = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["subscription_id", "subscription_name", "status", "price", "billing_cycle"],
            "properties": {
                "subscription_id": {"bsonType": "string"},
                "subscription_name": {"bsonType": "string"},
                "status": {"enum": ["active", "cancelled", "past_due", "trialing"]},
                "price": {"bsonType": ["double", "int", "long", "decimal"]},
                "price_amount": {"bsonType": ["double", "int", "long", "decimal"]},
                "billing_cycle": {"enum": ["monthly", "quarterly", "yearly"]},
                "start_date": {"bsonType": ["date"]},
                "end_date": {"bsonType": ["date", "null"]},
                "started_at": {"bsonType": ["date", "null"]},
                "ended_at": {"bsonType": ["date", "null"]},
            },
        }
    }

    _ensure_collection_with_validator(db, "jobs", jobs_validator)
    _ensure_collection_with_validator(db, "estimates", estimates_validator)
    _ensure_collection_with_validator(db, "services", services_validator)
    _ensure_collection_with_validator(db, "hvacSystems", hvac_systems_validator)
    _ensure_collection_with_validator(db, "subscriptions", subscriptions_validator)
