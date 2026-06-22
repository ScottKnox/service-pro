import logging
from datetime import UTC, date, datetime
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from bson import ObjectId
from flask import abort
from pymongo import MongoClient
from pymongo.errors import PyMongoError, OperationFailure

from config import get_mongo_settings

logger = logging.getLogger(__name__)

_mongo_client = None
_validators_initialized = False

DEFAULT_PRICE_BOOK_CATEGORIES = {
    "part": [
        "Capacitors",
        "Contactors",
        "Motors — Condenser Fan",
        "Motors — Blower",
        "Igniters and Sensors",
        "Gas Valves",
        "Control Boards",
        "Thermostats and Controls",
        "Filters",
        "Refrigerant Components",
        "Electrical Components",
        "Miscellaneous Parts",
    ],
    "equipment": [
        "AC Systems",
        "Heat Pump Systems",
        "Gas Furnaces",
        "Air Handlers",
        "Mini Split Systems",
        "Package Units",
        "Other Equipment",
    ],
    "material": [
        "Refrigerant",
        "Duct Materials",
        "Sealants and Adhesives",
        "Drain and Condensate",
        "Chemicals and Cleaners",
        "Wire and Electrical",
        "Insulation",
        "Miscellaneous Materials",
    ],
}


def _inject_credentials_into_uri(uri: str) -> str:
    settings = get_mongo_settings()
    if not (settings.username and settings.password):
        return uri

    parts = urlsplit(uri)
    if "@" in parts.netloc:
        return uri

    username = quote_plus(settings.username)
    password = quote_plus(settings.password)
    netloc = f"{username}:{password}@{parts.netloc}"

    query = parts.query
    if settings.auth_source and parts.scheme == "mongodb":
        query_items = dict(parse_qsl(query, keep_blank_values=True))
        query_items.setdefault("authSource", settings.auth_source)
        query = urlencode(query_items)

    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def build_mongodb_uri() -> str:
    settings = get_mongo_settings()

    if settings.uri:
        return _inject_credentials_into_uri(settings.uri)

    if settings.host.startswith("mongodb://") or settings.host.startswith("mongodb+srv://"):
        return _inject_credentials_into_uri(settings.host)

    credentials = ""
    if settings.username and settings.password:
        username = quote_plus(settings.username)
        password = quote_plus(settings.password)
        credentials = f"{username}:{password}@"

    uri = f"mongodb://{credentials}{settings.host}:{settings.port}"
    if credentials:
        uri = f"{uri}/?authSource={quote_plus(settings.auth_source)}"
    return uri


def get_db():
    global _mongo_client
    global _validators_initialized
    if _mongo_client is None:
        _mongo_client = MongoClient(build_mongodb_uri(), serverSelectionTimeoutMS=3000)
    mongo_settings = get_mongo_settings()
    db = _mongo_client[mongo_settings.db_name]
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
        db.create_collection(
            collection_name,
            validator=validator,
            validationLevel="moderate",
            validationAction="warn",
        )
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


def _seed_default_price_book_categories(db):
    if "businesses" not in db.list_collection_names():
        return

    if "categories" not in db.list_collection_names():
        return

    category_collection = db.categories
    now = datetime.now(UTC)

    for business in db.businesses.find({}, {"_id": 1}):
        company_id = str(business.get("_id") or "").strip()
        if not company_id:
            continue

        if category_collection.count_documents({"company_id": company_id}, limit=1):
            continue

        documents = []
        for category_type, category_names in DEFAULT_PRICE_BOOK_CATEGORIES.items():
            for sort_order, category_name in enumerate(category_names):
                documents.append(
                    {
                        "company_id": company_id,
                        "type": category_type,
                        "name": category_name,
                        "is_default": True,
                        "sort_order": sort_order,
                        "created_at": now,
                    }
                )

        if documents:
            category_collection.insert_many(documents)


def ensure_collection_validators(db):
    jobs_validator = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["customer_id", "status", "services", "total_amount", "payment_due_days"],
            "properties": {
                "customer_id": {"bsonType": ["objectId"]},
                "business_id": {"bsonType": ["objectId", "null"]},
                "status": {"enum": ["Pending", "Scheduled", "En Route", "Started", "Completed", "Paid"]},
                "payment_status": {"enum": ["pending_paid", "partial_paid", "paid", None]},
                "job_kind": {"enum": ["one_time", "recurring_occurrence", "series_template"]},
                "series_id": {"bsonType": ["objectId", "null"]},
                "occurrence_index": {"bsonType": ["int", "long", "null"]},
                "recurrence_summary": {"bsonType": ["string", "null"]},
                "invoice_notes": {"bsonType": ["string", "null"]},
                "services": {"bsonType": "array"},
                "parts": {"bsonType": "array"},
                "materials": {"bsonType": "array"},
                "labors": {"bsonType": "array"},
                "equipments": {"bsonType": "array"},
                "discounts": {"bsonType": "array"},
                "total_amount": {"bsonType": ["double", "int", "long", "decimal"]},
                "total_amount_paid": {"bsonType": ["double", "int", "long", "decimal", "null"]},
                "balance_due": {"bsonType": ["double", "int", "long", "decimal", "null"]},
                "payment_due_days": {"bsonType": ["int", "long"]},
                "created_at": {"bsonType": ["date"]},
                "scheduled_at": {"bsonType": ["date", "null"]},
                "completed_at": {"bsonType": ["date", "null"]},
                "paid_at": {"bsonType": ["date", "null"]},
            },
        }
    }

    payments_validator = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": [
                "job_id",
                "invoice_id",
                "company_id",
                "customer_id",
                "amount",
                "payment_method",
                "status",
                "paid_at",
                "recorded_by",
                "created_at",
            ],
            "properties": {
                "job_id": {"bsonType": ["objectId", "string"]},
                "invoice_id": {"bsonType": ["string"]},
                "company_id": {"bsonType": ["objectId", "string", "null"]},
                "customer_id": {"bsonType": ["objectId", "string"]},
                "amount": {"bsonType": ["double", "int", "long", "decimal"]},
                "payment_method": {"enum": ["card", "ach", "cash", "check"]},
                "stripe_payment_intent_id": {"bsonType": ["string", "null"]},
                "check_number": {"bsonType": ["string", "null"]},
                "status": {"enum": ["completed", "failed", "refunded"]},
                "paid_at": {"bsonType": ["date"]},
                "recorded_by": {"bsonType": ["objectId", "string", "null"]},
                "notes": {"bsonType": ["string", "null"]},
                "quickbooks_payment_id": {"bsonType": ["string", "null"]},
                "synced_to_quickbooks_at": {"bsonType": ["date", "null"]},
                "created_at": {"bsonType": ["date"]},
            },
        }
    }

    recurring_job_series_validator = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["customer_id", "business_id", "status", "frequency", "anchor_date", "services", "total_amount"],
            "properties": {
                "customer_id": {"bsonType": ["objectId"]},
                "business_id": {"bsonType": ["objectId", "null"]},
                "status": {"enum": ["Active", "Paused", "Cancelled"]},
                "frequency": {"enum": ["weekly", "biweekly", "monthly", "quarterly", "semiannual", "annual"]},
                "anchor_date": {"bsonType": "string"},
                "anchor_time": {"bsonType": ["string", "null"]},
                "end_type": {"enum": ["never", "on_date", "after_occurrences", None]},
                "end_date": {"bsonType": ["string", "null"]},
                "max_occurrences": {"bsonType": ["int", "long", "null"]},
                "next_occurrence_date": {"bsonType": ["string", "null"]},
                "last_generated_occurrence_index": {"bsonType": ["int", "long", "null"]},
                "services": {"bsonType": "array"},
                "parts": {"bsonType": "array"},
                "materials": {"bsonType": "array"},
                "labors": {"bsonType": "array"},
                "equipments": {"bsonType": "array"},
                "discounts": {"bsonType": "array"},
                "total_amount": {"bsonType": ["double", "int", "long", "decimal"]},
                "assigned_employee": {"bsonType": ["string", "null"]},
                "invoice_notes": {"bsonType": ["string", "null"]},
                "payment_due_days_offset": {"bsonType": ["int", "long", "null"]},
                "property_id": {"bsonType": ["objectId", "string", "null"]},
                "property_name": {"bsonType": ["string", "null"]},
                "address_line_1": {"bsonType": ["string", "null"]},
                "address_line_2": {"bsonType": ["string", "null"]},
                "city": {"bsonType": ["string", "null"]},
                "state": {"bsonType": ["string", "null"]},
                "zip_code": {"bsonType": ["string", "null"]},
                "created_at": {"bsonType": ["date", "null"]},
            },
        }
    }

    estimates_validator = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["customer_id", "status", "services", "total_amount", "estimate_expiration_days", "proposed_job_date", "proposed_job_time"],
            "properties": {
                "customer_id": {"bsonType": ["objectId"]},
                "status": {"enum": ["Created", "Sent", "Accepted", "Declined"]},
                "services": {"bsonType": "array"},
                "parts": {"bsonType": "array"},
                "materials": {"bsonType": "array"},
                "labors": {"bsonType": "array"},
                "equipments": {"bsonType": "array"},
                "discounts": {"bsonType": "array"},
                "estimate_notes": {"bsonType": ["string", "null"]},
                "estimate_expiration_days": {"bsonType": ["int", "long"]},
                "proposed_job_date": {"bsonType": ["string"]},
                "proposed_job_time": {"bsonType": ["string"]},
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
                "photos": {"bsonType": ["array", "null"]},
            },
        }
    }

    hvac_diagnostics_validator = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["hvac_system_id", "customer_id", "date_performed", "created_at"],
            "properties": {
                "hvac_system_id": {"bsonType": ["objectId", "string"]},
                "customer_id": {"bsonType": ["objectId", "string"]},
                "property_id": {"bsonType": ["objectId", "string", "null"]},
                "date_performed": {"bsonType": "string"},
                "created_at": {"bsonType": ["date"]},
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

    categories_validator = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["company_id", "type", "name", "is_default", "sort_order", "created_at"],
            "properties": {
                "company_id": {"bsonType": "string"},
                "type": {"enum": ["part", "equipment", "material"]},
                "name": {"bsonType": "string"},
                "is_default": {"bsonType": "bool"},
                "sort_order": {"bsonType": ["int", "long"]},
                "created_at": {"bsonType": ["date"]},
            },
        }
    }

    maintenance_plan_templates_validator = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["business_id", "name", "is_active", "visits_per_year", "price_annual", "created_at"],
            "properties": {
                "business_id": {"bsonType": ["objectId", "string"]},
                "name": {"bsonType": "string"},
                "description": {"bsonType": ["string", "null"]},
                "tier_order": {"bsonType": ["int", "long", "null"]},
                "is_active": {"bsonType": "bool"},
                "visits_per_year": {"bsonType": ["int", "long"]},
                "visit_seasons": {
                    "bsonType": ["array", "null"],
                    "items": {
                        "bsonType": "object",
                        "required": ["season", "service_id", "service_name"],
                        "properties": {
                            "season": {"bsonType": "string"},
                            "service_id": {"bsonType": ["objectId", "string", "null"]},
                            "service_name": {"bsonType": ["string", "null"]},
                            "start_date": {"bsonType": ["string", "null"]},
                            "end_date": {"bsonType": ["string", "null"]},
                        },
                    },
                },
                "price_annual": {"bsonType": ["double", "int", "long", "decimal"]},
                "price_monthly": {"bsonType": ["double", "int", "long", "decimal", "null"]},
                "repair_discount_pct": {"bsonType": ["double", "int", "long", "decimal", "null"]},
                "discount_service_types": {"bsonType": ["array", "null"]},
                "discount_line_item_types": {"bsonType": ["array", "null"]},
                "diagnostic_fee_waived": {"bsonType": ["bool", "null"]},
                "priority_scheduling": {"bsonType": ["bool", "null"]},
                "emergency_service": {"bsonType": ["bool", "null"]},
                "custom_benefits": {"bsonType": ["array", "null"]},
                "created_at": {"bsonType": ["date"]},
                "updated_at": {"bsonType": ["date", "null"]},
            },
        }
    }

    maintenance_plans_validator = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": [
                "plan_number",
                "business_id",
                "template_id",
                "template_snapshot",
                "customer_id",
                "customer_name",
                "company",
                "property_id",
                "property_name",
                "property_address",
                "covered_systems",
                "status",
                "start_date",
                "end_date",
                "renewal_date",
                "auto_renew",
                "billing_type",
                "billing_amount",
                "next_billing_date",
                "billing_history",
                "series_ids",
                "visits_scheduled",
                "visits_completed",
                "sold_by_employee_id",
                "sold_by_name",
                "sold_via",
                "created_at",
                "updated_at",
            ],
            "properties": {
                "plan_number": {"bsonType": "string"},
                "business_id": {"bsonType": ["objectId", "string"]},
                "template_id": {"bsonType": ["objectId", "string"]},
                "template_snapshot": {"bsonType": "object"},
                "customer_id": {"bsonType": ["objectId", "string"]},
                "customer_name": {"bsonType": "string"},
                "company": {"bsonType": "string"},
                "property_id": {"bsonType": ["objectId", "string"]},
                "property_name": {"bsonType": "string"},
                "property_address": {
                    "bsonType": "object",
                    "required": ["address_line_1", "address_line_2", "city", "state", "zip_code"],
                    "properties": {
                        "address_line_1": {"bsonType": "string"},
                        "address_line_2": {"bsonType": ["string", "null"]},
                        "city": {"bsonType": "string"},
                        "state": {"bsonType": "string"},
                        "zip_code": {"bsonType": "string"},
                    },
                },
                "covered_systems": {
                    "bsonType": ["array", "null"],
                    "items": {
                        "bsonType": "object",
                        "required": [
                            "hvac_system_id",
                            "system_nickname",
                            "system_type",
                            "system_tonnage",
                            "manufacturer",
                            "manufactured_year",
                        ],
                        "properties": {
                            "hvac_system_id": {"bsonType": ["objectId", "string"]},
                            "system_nickname": {"bsonType": "string"},
                            "system_type": {"bsonType": "string"},
                            "system_tonnage": {"bsonType": "string"},
                            "manufacturer": {"bsonType": "string"},
                            "manufactured_year": {"bsonType": "string"},
                        },
                    },
                },
                "status": {"enum": ["active", "pending", "lapsed", "cancelled", "expired"]},
                "start_date": {"bsonType": ["date"]},
                "end_date": {"bsonType": ["date"]},
                "renewal_date": {"bsonType": ["date"]},
                "auto_renew": {"bsonType": "bool"},
                "billing_type": {"enum": ["monthly", "annual"]},
                "billing_amount": {"bsonType": ["double", "int", "long", "decimal"]},
                "next_billing_date": {"bsonType": ["date"]},
                "billing_history": {
                    "bsonType": ["array", "null"],
                    "items": {
                        "bsonType": "object",
                        "required": ["date", "amount", "status", "invoice_id"],
                        "properties": {
                            "date": {"bsonType": ["date"]},
                            "amount": {"bsonType": ["double", "int", "long", "decimal"]},
                            "status": {"bsonType": "string"},
                            "invoice_id": {"bsonType": ["objectId", "string", "null"]},
                        },
                    },
                },
                "series_ids": {
                    "bsonType": ["array", "null"],
                    "items": {"bsonType": ["objectId", "string"]},
                },
                "visits_scheduled": {"bsonType": ["int", "long"]},
                "visits_completed": {"bsonType": ["int", "long"]},
                "last_visit_date": {"bsonType": ["date", "null"]},
                "next_visit_date": {"bsonType": ["date", "null"]},
                "sold_by_employee_id": {"bsonType": "string"},
                "sold_by_name": {"bsonType": "string"},
                "sold_via": {"enum": ["office", "tech_in_field", "customer_portal"]},
                "created_at": {"bsonType": ["date"]},
                "updated_at": {"bsonType": ["date"]},
                "cancelled_at": {"bsonType": ["date", "null"]},
                "cancellation_reason": {"bsonType": ["string", "null"]},
            },
        }
    }

    _ensure_collection_with_validator(db, "jobs", jobs_validator)
    _ensure_collection_with_validator(db, "payments", payments_validator)
    _ensure_collection_with_validator(db, "recurring_job_series", recurring_job_series_validator)
    _ensure_collection_with_validator(db, "estimates", estimates_validator)
    _ensure_collection_with_validator(db, "services", services_validator)
    _ensure_collection_with_validator(db, "hvacSystems", hvac_systems_validator)
    _ensure_collection_with_validator(db, "hvacDiagnostics", hvac_diagnostics_validator)
    _ensure_collection_with_validator(db, "subscriptions", subscriptions_validator)
    _ensure_collection_with_validator(db, "categories", categories_validator)
    _ensure_collection_with_validator(db, "maintenance_plan_templates", maintenance_plan_templates_validator)
    _ensure_collection_with_validator(db, "maintenance_plans", maintenance_plans_validator)

    db.hvacDiagnostics.create_index([("hvac_system_id", 1), ("created_at", -1)])
    db.hvacDiagnostics.create_index([("customer_id", 1), ("created_at", -1)])
    db.jobs.create_index([("customer_id", 1), ("property_id", 1), ("status", 1), ("completed_at", -1)])
    db.jobs.create_index([("customer_id", 1), ("status", 1), ("completed_at", -1)])
    db.jobs.create_index([("business_id", 1), ("property_id", 1), ("status", 1), ("completed_at", -1)])
    db.jobs.create_index([("company_id", 1), ("property_id", 1), ("status", 1), ("completed_at", -1)])
    db.jobs.create_index([("business_id", 1), ("services.hvac_system_id", 1), ("status", 1), ("completed_at", -1)])
    db.jobs.create_index([("business_id", 1), ("parts.hvac_system_id", 1), ("status", 1), ("completed_at", -1)])
    db.jobs.create_index([("business_id", 1), ("labors.hvac_system_id", 1), ("status", 1), ("completed_at", -1)])
    db.jobs.create_index([("business_id", 1), ("materials.hvac_system_id", 1), ("status", 1), ("completed_at", -1)])
    db.jobs.create_index([("business_id", 1), ("equipments.hvac_system_id", 1), ("status", 1), ("completed_at", -1)])
    db.jobs.create_index([("company_id", 1), ("services.hvac_system_id", 1), ("status", 1), ("completed_at", -1)])
    db.jobs.create_index([("company_id", 1), ("parts.hvac_system_id", 1), ("status", 1), ("completed_at", -1)])
    db.jobs.create_index([("company_id", 1), ("labors.hvac_system_id", 1), ("status", 1), ("completed_at", -1)])
    db.jobs.create_index([("company_id", 1), ("materials.hvac_system_id", 1), ("status", 1), ("completed_at", -1)])
    db.jobs.create_index([("company_id", 1), ("equipments.hvac_system_id", 1), ("status", 1), ("completed_at", -1)])
    db.categories.create_index([("company_id", 1), ("type", 1), ("sort_order", 1)])
    db.maintenance_plan_templates.create_index([("business_id", 1), ("is_active", 1), ("tier_order", 1)])
    db.maintenance_plans.create_index([("business_id", 1), ("status", 1), ("renewal_date", 1)])
    db.maintenance_plans.create_index([("business_id", 1), ("customer_id", 1), ("status", 1)])
    db.maintenance_plans.create_index([("business_id", 1), ("property_id", 1), ("status", 1)])
    db.maintenance_plans.create_index([("template_id", 1)])
    db.payments.create_index([("job_id", 1), ("paid_at", -1)])
    db.payments.create_index([("customer_id", 1), ("paid_at", -1)])
    db.payments.create_index([("invoice_id", 1), ("paid_at", -1)])
    db.payments.create_index([("status", 1), ("paid_at", -1)])
    db.password_resets.create_index([("token_hash", 1)])
    db.password_resets.create_index([("employee_id", 1), ("used", 1)])
    db.password_resets.create_index([("expires_at", 1)], expireAfterSeconds=0)
    db.password_reset_attempts.create_index([("ip", 1), ("created_at", -1)])
    db.password_reset_attempts.create_index([("created_at", 1)], expireAfterSeconds=3600)
    db.login_attempts.create_index([("ip", 1), ("created_at", -1)])
    db.login_attempts.create_index([("created_at", 1)], expireAfterSeconds=3600)

    _seed_default_price_book_categories(db)
