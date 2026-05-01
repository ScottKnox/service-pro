from datetime import datetime
import json
import os
import re

from bson import ObjectId
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for
from flask_mail import Message
from werkzeug.utils import secure_filename

ALLOWED_PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "heic"}
MAX_PHOTO_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
HVAC_PHOTO_UPLOAD_SUBDIR = os.path.join("uploads", "hvac_photos")
MAX_PHOTO_CAPTION_LENGTH = 220

from mongo import build_reference_filter, ensure_connection_or_500, object_id_or_404, reference_value, serialize_doc
from hvac_report_generator import generate_hvac_system_health_report
from utils.catalog import build_job_parts_from_form, build_part_catalog
from utils.csv_export import build_csv_export_response

bp = Blueprint("customers", __name__)


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

EMAIL_VALIDATION_MESSAGE = "Enter a valid email address."
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

CUSTOMER_TYPE_OPTIONS = (
    "Residential",
    "Commercial",
    "Industrial",
    "Institutional",
    "Specialty",
)

PROPERTY_TYPE_OPTIONS_BY_CUSTOMER_TYPE = {
    "Residential": [
        "single_family_home",
        "condo_townhouse",
        "apartment",
        "mobile_home",
        "new_construction",
        "other",
    ],
    "Commercial": [
        "office_building",
        "retail_store",
        "restaurant",
        "hotel",
        "medical_office",
        "gym",
        "car_dealership",
        "bank",
        "other",
    ],
    "Industrial": [
        "warehouse",
        "manufacturing_facility",
        "refrigeration_facility",
        "data_center",
        "auto_shop",
        "other",
    ],
    "Institutional": [
        "school",
        "hospital",
        "government_building",
        "church",
        "community_center",
        "other",
    ],
    "Specialty": [
        "multi_tenant_commercial_building",
        "hoa_managed_community",
        "property_management_portfolio",
        "new_construction_builder_contract",
        "other",
    ],
}

PROPERTY_TYPE_LABELS = {
    "single_family_home": "Single-family home",
    "condo_townhouse": "Condo & townhouse",
    "apartment": "Apartment",
    "mobile_home": "Mobile home",
    "new_construction": "New construction",
    "office_building": "Office building",
    "retail_store": "Retail store",
    "restaurant": "Restaurant",
    "hotel": "Hotel",
    "medical_office": "Medical office",
    "gym": "Gym",
    "car_dealership": "Car dealership",
    "bank": "Bank",
    "warehouse": "Warehouse",
    "manufacturing_facility": "Manufacturing facility",
    "refrigeration_facility": "Refrigeration facility",
    "data_center": "Data center",
    "auto_shop": "Auto shop",
    "school": "School",
    "hospital": "Hospital",
    "government_building": "Government building",
    "church": "Church",
    "community_center": "Community center",
    "multi_tenant_commercial_building": "Multi-tenant commercial building (landlord contract)",
    "hoa_managed_community": "HOA-managed community",
    "property_management_portfolio": "Property management portfolio (one contract covering many units)",
    "new_construction_builder_contract": "New construction (builder contract - ongoing relationship across multiple builds)",
    "other": "Other",
}


def _normalize_customer_type(raw_type):
    customer_type = str(raw_type or "").strip().title()
    if customer_type in CUSTOMER_TYPE_OPTIONS:
        return customer_type
    return "Residential"


def _get_property_type_options(customer_type):
    normalized_type = _normalize_customer_type(customer_type)
    return PROPERTY_TYPE_OPTIONS_BY_CUSTOMER_TYPE.get(normalized_type, PROPERTY_TYPE_OPTIONS_BY_CUSTOMER_TYPE["Residential"])


def _property_type_label(property_type):
    normalized_property_type = str(property_type or "").strip()
    if not normalized_property_type:
        return "-"
    return PROPERTY_TYPE_LABELS.get(normalized_property_type, normalized_property_type.replace("_", " ").title())


def _get_customer_properties(customer):
    raw_properties = (customer or {}).get("properties", [])
    if not isinstance(raw_properties, list):
        return []

    normalized_properties = []
    for prop in raw_properties:
        if not isinstance(prop, dict):
            continue
        property_id = str(prop.get("property_id") or "").strip() or str(ObjectId())
        property_type = str(prop.get("property_type") or "").strip()
        is_default = bool(prop.get("is_default"))
        normalized_properties.append(
            {
                "property_id": property_id,
                "property_name": str(prop.get("property_name") or "").strip(),
                "property_type": property_type,
                "property_type_label": _property_type_label(property_type),
                "property_type_other": str(prop.get("property_type_other") or "").strip(),
                "address_line_1": str(prop.get("address_line_1") or "").strip(),
                "address_line_2": str(prop.get("address_line_2") or "").strip(),
                "city": str(prop.get("city") or "").strip(),
                "state": str(prop.get("state") or "").strip().upper(),
                "zip_code": str(prop.get("zip_code") or "").strip(),
                "is_default": is_default,
                "is_seed_primary_address": bool(prop.get("is_seed_primary_address")),
            }
        )

    # Enforce a single default property in-memory for rendering and downstream logic.
    default_count = sum(1 for prop in normalized_properties if prop.get("is_default"))
    if default_count > 1:
        first_default_found = False
        for prop in normalized_properties:
            if prop.get("is_default") and not first_default_found:
                first_default_found = True
                continue
            prop["is_default"] = False

    return normalized_properties


def _find_customer_property(customer, property_id):
    normalized_property_id = str(property_id or "").strip()
    if not normalized_property_id:
        return None

    for prop in _get_customer_properties(customer):
        if prop.get("property_id") == normalized_property_id:
            return prop
    return None


def _normalize_property_payload(form_data, customer_type):
    property_type = str(form_data.get("property_type") or "").strip()
    property_type_other = str(form_data.get("property_type_other") or "").strip()
    if property_type == "other" and property_type_other:
        property_type_value = property_type_other
    else:
        property_type_value = property_type

    return {
        "property_id": str(form_data.get("property_id") or "").strip() or str(ObjectId()),
        "property_name": str(form_data.get("property_name") or "").strip(),
        "property_type": property_type_value,
        "property_type_other": property_type_other,
        "address_line_1": str(form_data.get("address_line_1") or "").strip(),
        "address_line_2": str(form_data.get("address_line_2") or "").strip(),
        "city": str(form_data.get("city") or "").strip(),
        "state": str(form_data.get("state") or "").strip().upper(),
        "zip_code": str(form_data.get("zip_code") or "").strip(),
        "is_default": bool(form_data.get("is_default")),
        "is_seed_primary_address": bool(form_data.get("is_seed_primary_address")),
    }


def _property_payload_is_valid(property_payload):
    required_values = (
        property_payload.get("property_name"),
        property_payload.get("property_type"),
        property_payload.get("address_line_1"),
        property_payload.get("city"),
        property_payload.get("state"),
        property_payload.get("zip_code"),
    )
    return all(str(value or "").strip() for value in required_values)

HVAC_COLLECTION_CONFIG = {
    "Split System AC with Gas Furnace": (
        ("condensers", "Condenser"),
        ("furnaces", "Furnace"),
        ("thermostats", "Thermostat"),
        ("refrigerants", "Refrigerant"),
    ),
    "Split System Heat Pump with Air Handler": (
        ("condensers", "Condenser"),
        ("airHandlers", "Air Handler"),
        ("thermostats", "Thermostat"),
        ("refrigerants", "Refrigerant"),
    ),
    "Split System Heat Pump with Gas Furnace": (
        ("condensers", "Condenser"),
        ("furnaces", "Furnace"),
        ("thermostats", "Thermostat"),
        ("refrigerants", "Refrigerant"),
    ),
    "Mini Split System": (
        ("miniSplits", "Unit"),
        ("refrigerants", "Refrigerant"),
    ),
    "Package Unit": (
        ("packageUnits", "Unit"),
        ("thermostats", "Thermostat"),
        ("refrigerants", "Refrigerant"),
    ),
}

HVAC_COMPONENT_LABELS = {
    "airHandlers": "Air Handler",
    "condensers": "Condenser",
    "furnaces": "Furnace",
    "thermostats": "Thermostat",
    "refrigerants": "Refrigerant",
    "packageUnits": "Unit",
    "miniSplits": "Unit",
}

HVAC_COMPONENT_FIELD_BY_COLLECTION = {
    "airHandlers": "air_handler",
    "condensers": "condenser",
    "furnaces": "furnace",
    "thermostats": "thermostat",
    "refrigerants": "refrigerant",
    "packageUnits": "unit",
    "miniSplits": "unit",
}

SYSTEM_TYPE_OPTIONS = (
    "Split System AC with Gas Furnace",
    "Split System Heat Pump with Air Handler",
    "Split System Heat Pump with Gas Furnace",
    "Mini Split System",
    "Package Unit",
)

TONNAGE_OPTIONS = (
    ".50 Ton (6000 BTU)",
    ".75 Ton (9000 BTU)",
    "1 Ton (12000 BTU)",
    "1.5 Tons (18000 BTU)",
    "2 Ton (24000 BTU)",
    "2.5 Tons (30000 BTU)",
    "3 Ton (36000 BTU)",
    "3.5 Tons (42000 BTU)",
    "4 Ton (48000 BTU)",
    "4.5 Ton (54000 BTU)",
    "5 Ton (60000 BTU)",
)

DUCTWORK_SYSTEM_TYPES = {
    "Split System AC with Gas Furnace",
    "Split System Heat Pump with Air Handler",
    "Split System Heat Pump with Gas Furnace",
    "Package Unit",
}

SINGLE_TONNAGE_SYSTEM_TYPES = {
    "Split System Heat Pump with Air Handler",
    "Mini Split System",
}

DUCTWORK_TYPE_OPTIONS = (
    "Sheet Metal and Flex",
    "Fiberglass Lined Sheet Metal and Flex",
    "Sheet Metal",
    "Flex",
    "Fiberboard",
)

INSULATED_OPTIONS = ("No", "Yes")

REFRIGERANT_TYPE_OPTIONS = (
    "R-22",
    "R-410A",
    "R-32",
    "R-454B",
    "R-134a",
    "R-407C",
    "Other",
)

CONTACTOR_CONDITION_OPTIONS = (
    "Good",
    "Questionable",
    "Bad",
)

LOW_VOLTAGE_24V_OPTIONS = (
    "Good",
    "Below 24V",
    "Above 24V",
)

GROUND_WIRE_PRESENT_OPTIONS = (
    "Yes",
    "No",
)

DIAGNOSTIC_YES_NO_OPTIONS = (
    "Yes",
    "No",
)

MANUFACTURER_OPTIONS = (
    "Trane",
    "Lennox",
    "Carrier",
    "Goodman",
    "Amana",
    "Rheem",
    "Daikin",
    "American Standard",
    "York",
    "Mitsubishi",
    "Bryant",
    "Payne",
)

MINI_SPLIT_MANUFACTURER_OPTIONS = (
    "Mitsubishi",
    "Daikin",
    "LG",
    "Fujitsu",
    "Gree",
    "Pioneer",
    "Other",
)

THERMOSTAT_TYPE_OPTIONS = (
    "Programmable",
    "Smart",
    "Manual",
    "Communicating",
)

THERMOSTAT_MANUFACTURER_OPTIONS = (
    "Ecobee",
    "Honeywell",
    "Nest",
    "Emerson",
    "Other",
)

PACKAGE_UNIT_TYPE_OPTIONS = (
    "Gas Electric",
    "Heat Pump",
    "Dual Fuel",
    "Electric Only",
)

HVAC_DIAGNOSTIC_SECTIONS = (
    (
        "Airflow",
        (
            {"name": "supplyStaticPressure", "label": "Supply Static Pressure", "type": "text", "required": False},
            {"name": "returnStaticPressure", "label": "Return Static Pressure", "type": "text", "required": False},
            {
                "name": "totalExternalStaticPressure",
                "label": "Total External Static Pressure",
                "type": "text",
                "required": False,
                "readonly": True,
            },
            {
                "name": "designStaticPressure",
                "label": "Design Static Pressure",
                "type": "text",
                "required": False,
            },
            {"name": "temperatureDelta", "label": "Temperature Delta", "type": "text", "required": False},
            {
                "name": "staticPressureNotes",
                "label": "Static Pressure Notes",
                "type": "textarea",
                "required": False,
            },
        ),
    ),
    (
        "Refrigerant",
        (
            {
                "name": "refrigerantType",
                "label": "Refrigerant Type",
                "type": "select",
                "required": False,
                "options": REFRIGERANT_TYPE_OPTIONS,
            },
            {"name": "suctionPressure", "label": "Suction Pressure", "type": "text", "required": False},
            {"name": "dischargePressure", "label": "Discharge Pressure", "type": "text", "required": False},
            {"name": "superheat", "label": "Superheat", "type": "text", "required": False},
            {"name": "targetSuperheat", "label": "Target Superheat", "type": "text", "required": False},
            {"name": "subcooling", "label": "Subcooling", "type": "text", "required": False},
            {"name": "targetSubcooling", "label": "Target Subcooling", "type": "text", "required": False},
            {
                "name": "notesOnRefrigerant",
                "label": "Notes on Refrigerant",
                "type": "textarea",
                "required": False,
            },
        ),
    ),
    (
        "Electrical",
        (
            {
                "name": "acCapacitorVoltage",
                "label": "AC Capacitor Voltage",
                "type": "text",
                "required": False,
            },
            {
                "name": "targetAcCapacitorVoltage",
                "label": "Target AC Capacitor Voltage",
                "type": "text",
                "required": False,
            },
            {
                "name": "compressorAmperage",
                "label": "Compressor Amperage",
                "type": "text",
                "required": False,
            },
            {
                "name": "targetCompressorAmperage",
                "label": "Target Compressor Amperage",
                "type": "text",
                "required": False,
            },
            {
                "name": "outdoorDisconnectVoltage",
                "label": "Outdoor Disconnect Voltage",
                "type": "text",
                "required": False,
            },
            {
                "name": "contactorCondition",
                "label": "Contactor Condition",
                "type": "select",
                "required": False,
                "options": CONTACTOR_CONDITION_OPTIONS,
            },
            {
                "name": "lowVoltage24V",
                "label": "Low Voltage - 24V",
                "type": "select",
                "required": False,
                "options": LOW_VOLTAGE_24V_OPTIONS,
            },
            {
                "name": "groundWirePresent",
                "label": "Ground Wire Present",
                "type": "select",
                "required": False,
                "options": GROUND_WIRE_PRESENT_OPTIONS,
            },
            {
                "name": "electricalNotes",
                "label": "Electrical Notes",
                "type": "textarea",
                "required": False,
            },
        ),
    ),
    (
        "Ductwork",
        (
            {
                "name": "properlySized",
                "label": "Properly Sized?",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
            {
                "name": "leaks",
                "label": "Leaks?",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
            {
                "name": "wellInsulated",
                "label": "Well-Insulated?",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
            {
                "name": "wellSupported",
                "label": "Well-Supported?",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
            {
                "name": "damaged",
                "label": "Damaged?",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
            {
                "name": "clean",
                "label": "Clean?",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
            {
                "name": "dampersFunctioningProperly",
                "label": "Dampers Functioning Properly?",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
            {
                "name": "notesOnDuctwork",
                "label": "Notes on Ductwork",
                "type": "textarea",
                "required": False,
            },
        ),
    ),
    (
        "Indoor Air Quality",
        (
            {
                "name": "relativeHumidity",
                "label": "Relative Humidity",
                "type": "text",
                "required": False,
            },
            {
                "name": "carbonDioxide",
                "label": "Carbon Dioxide (CO2)",
                "type": "text",
                "required": False,
            },
            {
                "name": "carbonMonoxide",
                "label": "Carbon Monoxide",
                "type": "text",
                "required": False,
            },
            {
                "name": "vocLevels",
                "label": "VOC Levels",
                "type": "text",
                "required": False,
            },
            {
                "name": "pm25",
                "label": "PM 2.5",
                "type": "text",
                "required": False,
            },
            {
                "name": "pm10",
                "label": "PM10",
                "type": "text",
                "required": False,
            },
            {
                "name": "moldOrMildew",
                "label": "Mold / Mildew",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
        ),
    ),
)

HVAC_DIAGNOSTIC_FIELDS = tuple(
    (field["name"], field["label"])
    for _section_label, fields in HVAC_DIAGNOSTIC_SECTIONS
    for field in fields
)

HVAC_DIAGNOSTIC_FIELD_TYPES = {
    field["name"]: field.get("type", "text")
    for _section_label, fields in HVAC_DIAGNOSTIC_SECTIONS
    for field in fields
}

HVAC_FORM_PREFIX_BY_COLLECTION = {
    "airHandlers": "air_handler",
    "condensers": "condenser",
    "furnaces": "furnace",
    "thermostats": "thermostat",
    "refrigerants": "refrigerant",
    "packageUnits": "unit",
    "miniSplits": "unit",
}


def _email_is_valid(email):
    return bool(EMAIL_PATTERN.match(email))


def _build_hvac_component(form_data, prefix):
    component = {
        "serial_number": form_data.get(f"{prefix}_serial_number", "").strip(),
        "model_number": form_data.get(f"{prefix}_model_number", "").strip(),
        "manufacturer": form_data.get(f"{prefix}_manufacturer", "").strip(),
        "manufacturer_other": form_data.get(f"{prefix}_manufacturer_other", "").strip(),
        "install_year": form_data.get(f"{prefix}_install_year", "").strip(),
        "nickname": form_data.get(f"{prefix}_nickname", "").strip(),
    }

    if prefix == "unit":
        component["unit_type"] = form_data.get("unit_type", "").strip()

    if prefix == "thermostat":
        component["thermostat_type"] = form_data.get("thermostat_type", "").strip()

    if prefix == "refrigerant":
        component["refrigerant_type"] = form_data.get("refrigerant_type", "").strip()

    return component


def _build_hvac_ductwork(form_data):
    ductwork = {
        "type": form_data.get("ductwork_type", "").strip(),
        "insulated": form_data.get("ductwork_insulated", "").strip(),
        "supply_branches": form_data.get("ductwork_supply_branches", "").strip(),
        "returns": form_data.get("ductwork_returns", "").strip(),
        "ductwork_notes": form_data.get("ductwork_notes", "").strip(),
    }
    return ductwork if any(ductwork.values()) else None


def _extract_hvac_ductwork(source):
    if not isinstance(source, dict):
        return None

    ductwork = source.get("ductwork")
    if not isinstance(ductwork, dict):
        return None

    normalized_ductwork = {
        "type": str(ductwork.get("type", "")).strip(),
        "insulated": str(ductwork.get("insulated", "")).strip(),
        "supply_branches": str(ductwork.get("supply_branches", "")).strip(),
        "returns": str(ductwork.get("returns", "")).strip(),
        "ductwork_notes": str(ductwork.get("ductwork_notes", "")).strip(),
    }
    return normalized_ductwork if any(normalized_ductwork.values()) else None


def _build_hvac_system_document(customer_id, system_type, form_data):
    document = {
        "customer_id": reference_value(customer_id),
        "system_type": system_type,
    }

    property_id = str(form_data.get("property_id") or "").strip()
    if property_id:
        document["property_id"] = reference_value(property_id)

    if system_type in SINGLE_TONNAGE_SYSTEM_TYPES:
        system_tonnage = str(form_data.get("system_tonnage", "")).strip()
        if system_tonnage:
            document["system_tonnage"] = system_tonnage
    else:
        cooling_capacity = str(form_data.get("cooling_capacity", "")).strip()
        heating_capacity = str(form_data.get("heating_capacity", "")).strip()
        if cooling_capacity:
            document["cooling_capacity"] = cooling_capacity
        if heating_capacity:
            document["heating_capacity"] = heating_capacity

    if system_type in DUCTWORK_SYSTEM_TYPES:
        ductwork = _build_hvac_ductwork(form_data)
        if ductwork:
            document["ductwork"] = ductwork

    return document


def _get_missing_ductwork_fields(ductwork_data):
    ductwork_type = str(ductwork_data.get("type", "")).strip()
    if not ductwork_type:
        return []

    required_fields = ("insulated", "supply_branches", "returns")
    return [
        field_name
        for field_name in required_fields
        if not str(ductwork_data.get(field_name, "")).strip()
    ]


def _validate_ductwork_data(ductwork_data):
    required_fields = {
        "insulated": "Insulated",
        "supply_branches": "Supply Branches",
        "returns": "Returns",
    }
    missing_fields = [
        required_fields[field_name]
        for field_name in _get_missing_ductwork_fields(ductwork_data)
    ]
    if not missing_fields:
        return ""

    if len(missing_fields) == 1:
        return f"{missing_fields[0]} is required when a ductwork type is selected."

    if len(missing_fields) == 2:
        missing_text = f"{missing_fields[0]} and {missing_fields[1]}"
    else:
        missing_text = ", ".join(missing_fields[:-1]) + f", and {missing_fields[-1]}"

    return f"{missing_text} are required when a ductwork type is selected."


def _build_empty_hvac_form_data():
    return {
        "system_type": "",
        "system_tonnage": "",
        "cooling_capacity": "",
        "heating_capacity": "",
        "air_handler_model_number": "",
        "air_handler_serial_number": "",
        "air_handler_manufacturer": "",
        "air_handler_manufacturer_other": "",
        "air_handler_install_year": "",
        "air_handler_nickname": "",
        "condenser_model_number": "",
        "condenser_serial_number": "",
        "condenser_manufacturer": "",
        "condenser_manufacturer_other": "",
        "condenser_install_year": "",
        "condenser_nickname": "",
        "furnace_model_number": "",
        "furnace_serial_number": "",
        "furnace_manufacturer": "",
        "furnace_manufacturer_other": "",
        "furnace_install_year": "",
        "furnace_nickname": "",
        "unit_model_number": "",
        "unit_serial_number": "",
        "unit_manufacturer": "",
        "unit_manufacturer_other": "",
        "unit_install_year": "",
        "unit_nickname": "",
        "unit_type": "",
        "thermostat_type": "",
        "thermostat_manufacturer": "",
        "thermostat_manufacturer_other": "",
        "thermostat_nickname": "",
        "refrigerant_type": "",
        "ductwork_type": "",
        "ductwork_insulated": "",
        "ductwork_supply_branches": "",
        "ductwork_returns": "",
        "ductwork_notes": "",
        "property_id": "",
    }


def _find_existing_hvac_component(db, customer_id, hvac_system, collection_name):
    components = hvac_system.get("components") if isinstance(hvac_system.get("components"), dict) else {}
    component = components.get(collection_name)
    return component if isinstance(component, dict) and component else None


def _build_hvac_component_document(form_data, component_base_document, collection_name):
    prefix = HVAC_FORM_PREFIX_BY_COLLECTION[collection_name]
    return {
        **component_base_document,
        **_build_hvac_component(form_data, prefix),
    }


def _summarize_hvac_component(component):
    summary_parts = []
    nickname = str(component.get("nickname", "")).strip()
    manufacturer = str(component.get("manufacturer", "")).strip()
    manufacturer_other = str(component.get("manufacturer_other", "")).strip()
    model_number = str(component.get("model_number", "")).strip()
    thermostat_type = str(component.get("thermostat_type", "")).strip()
    refrigerant_type = str(component.get("refrigerant_type", "")).strip()
    unit_type = str(component.get("unit_type", "")).strip()

    if manufacturer.lower() == "other" and manufacturer_other:
        manufacturer = manufacturer_other

    if nickname:
        summary_parts.append(nickname)

    if manufacturer:
        summary_parts.append(manufacturer)
    if model_number:
        summary_parts.append(model_number)
    if thermostat_type:
        summary_parts.append(thermostat_type)
    if refrigerant_type:
        summary_parts.append(refrigerant_type)
    if unit_type:
        summary_parts.append(unit_type)

    if not summary_parts:
        return "No component details saved yet."

    return " | ".join(summary_parts)


def _summarize_ductwork(component):
    ductwork = _extract_hvac_ductwork(component)
    if not ductwork:
        return ""

    summary_parts = []
    ductwork_type = str(ductwork.get("type", "")).strip()

    if ductwork_type:
        summary_parts.append(ductwork_type)

    return " | ".join(summary_parts)


def _format_hvac_component_detail(component):
    manufacturer = str(component.get("manufacturer", "")).strip()
    manufacturer_other = str(component.get("manufacturer_other", "")).strip()
    if manufacturer.lower() == "other" and manufacturer_other:
        display_manufacturer = manufacturer_other
    else:
        display_manufacturer = manufacturer

    return {
        "model_number": str(component.get("model_number", "")).strip() or "-",
        "serial_number": str(component.get("serial_number", "")).strip() or "-",
        "manufacturer": display_manufacturer or "-",
        "install_year": str(component.get("install_year", "")).strip() or "-",
        "nickname": str(component.get("nickname", "")).strip() or "-",
        "thermostat_type": str(component.get("thermostat_type", "")).strip() or "-",
        "refrigerant_type": str(component.get("refrigerant_type", "")).strip() or "-",
        "unit_type": str(component.get("unit_type", "")).strip() or "-",
        "notes": str(component.get("notes", "")).strip() or "-",
    }


def _format_diagnostics_key(key):
    return str(key).replace("_", " ").strip().title()


def _diagnostic_section_key(section_label):
    section_text = str(section_label or "").strip().lower()
    if not section_text:
        return "section"

    normalized = re.sub(r"[^a-z0-9]+", "_", section_text)
    normalized = normalized.strip("_")
    return normalized or "section"


def _build_hvac_photo_url(filename):
    safe_filename = str(filename or "").strip()
    if not safe_filename:
        return ""
    upload_path = os.path.join(HVAC_PHOTO_UPLOAD_SUBDIR, safe_filename).replace("\\", "/")
    return url_for("static", filename=upload_path)


def _normalize_diagnostic_photo_entry(raw_photo):
    if not isinstance(raw_photo, dict):
        return None

    filename = str(raw_photo.get("filename") or "").strip()
    url = str(raw_photo.get("url") or "").strip() or _build_hvac_photo_url(filename)
    if not filename and not url:
        return None

    return {
        "filename": filename,
        "url": url,
        "caption": str(raw_photo.get("caption") or "").strip(),
        "uploaded_at": str(raw_photo.get("uploaded_at") or "").strip(),
    }


def _build_diagnostic_section_photos_from_entry(diagnostic_entry):
    section_photos = {}
    raw_section_photos = (diagnostic_entry or {}).get("section_photos")
    if not isinstance(raw_section_photos, dict):
        return section_photos

    for section_key, photos in raw_section_photos.items():
        normalized_key = _diagnostic_section_key(section_key)
        if not isinstance(photos, list):
            continue

        normalized_photos = []
        for photo in photos:
            normalized_photo = _normalize_diagnostic_photo_entry(photo)
            if normalized_photo:
                normalized_photos.append(normalized_photo)

        if normalized_photos:
            section_photos[normalized_key] = normalized_photos

    return section_photos


def _resolve_section_photo_bucket(section_photos, section_key):
    if not isinstance(section_photos, dict):
        section_photos = {}

    normalized_target = _diagnostic_section_key(section_key)
    for existing_key, existing_photos in section_photos.items():
        if _diagnostic_section_key(existing_key) == normalized_target and isinstance(existing_photos, list):
            return section_photos, existing_key, existing_photos

    if normalized_target not in section_photos or not isinstance(section_photos.get(normalized_target), list):
        section_photos[normalized_target] = []

    return section_photos, normalized_target, section_photos[normalized_target]


_NUMERIC_VALUE_PATTERN = re.compile(r"[-+]?\d*\.?\d+")


def _parse_numeric_value(raw_value):
    if raw_value is None:
        return None

    if isinstance(raw_value, (int, float)):
        return float(raw_value)

    text = str(raw_value).strip().replace(",", "")
    if not text:
        return None

    match = _NUMERIC_VALUE_PATTERN.search(text)
    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def _normalize_condition_label(raw_condition):
    status_text = str(raw_condition or "").strip().lower()
    if not status_text:
        return None

    if "no data" in status_text or status_text in {"n/a", "na", "none", "unknown"}:
        return "No Data"
    if "within" in status_text or "normal" in status_text or status_text in {"ok", "good"}:
        return "Within Spec"
    if "low" in status_text:
        return "Low"
    if "high" in status_text or "alert" in status_text or "critical" in status_text or "fault" in status_text:
        return "High"

    return None


def _compare_to_target(actual_value, target_value, tolerance=0.0):
    if actual_value is None or target_value is None:
        return "No Data"

    if actual_value < (target_value - tolerance):
        return "Low"
    if actual_value > (target_value + tolerance):
        return "High"
    return "Within Spec"


def _compare_to_range(actual_value, min_value, max_value):
    if actual_value is None or min_value is None or max_value is None:
        return "No Data"

    if actual_value < min_value:
        return "Low"
    if actual_value > max_value:
        return "High"
    return "Within Spec"


def _merge_conditions(*conditions):
    filtered = [condition for condition in conditions if condition and condition != "No Data"]
    if not filtered:
        return "No Data"
    if "High" in filtered:
        return "High"
    if "Low" in filtered:
        return "Low"
    return "Within Spec"


def _derive_hvac_overall_conditions(values):
    values = values or {}

    delta_t_condition = _normalize_condition_label(
        values.get("temperatureDeltaStatus")
        or values.get("deltaTStatus")
        or values.get("temperatureDeltaRange")
    )
    if not delta_t_condition:
        delta_t_actual = _parse_numeric_value(values.get("temperatureDelta"))
        delta_t_target = _parse_numeric_value(values.get("targetTemperatureDelta") or values.get("designTemperatureDelta"))
        if delta_t_target is not None:
            delta_t_condition = _compare_to_target(delta_t_actual, delta_t_target)
        else:
            # Default HVAC delta-T comfort band when no explicit target exists.
            delta_t_condition = _compare_to_range(delta_t_actual, 16.0, 22.0)

    compressor_condition = _normalize_condition_label(
        values.get("compressorAmperageStatus")
        or values.get("compressorStatus")
        or values.get("compressorRange")
        or values.get("compressorLoadStatus")
    )
    if not compressor_condition:
        compressor_condition = _compare_to_target(
            _parse_numeric_value(values.get("compressorAmperage")),
            _parse_numeric_value(values.get("targetCompressorAmperage")),
        )

    static_pressure_condition = _normalize_condition_label(
        values.get("staticPressureStatus")
        or values.get("staticPressureRange")
    )
    if not static_pressure_condition:
        static_pressure_condition = _compare_to_target(
            _parse_numeric_value(values.get("totalExternalStaticPressure")),
            _parse_numeric_value(values.get("designStaticPressure")),
        )

    capacitor_condition = _normalize_condition_label(
        values.get("acCapacitorStatus")
        or values.get("capacitorStatus")
    )
    if not capacitor_condition:
        capacitor_condition = _compare_to_target(
            _parse_numeric_value(values.get("acCapacitorVoltage")),
            _parse_numeric_value(values.get("targetAcCapacitorVoltage")),
        )

    superheat_condition = _compare_to_target(
        _parse_numeric_value(values.get("superheat")),
        _parse_numeric_value(values.get("targetSuperheat")),
    )
    subcooling_condition = _compare_to_target(
        _parse_numeric_value(values.get("subcooling")),
        _parse_numeric_value(values.get("targetSubcooling")),
    )
    refrigerant_condition = _normalize_condition_label(
        values.get("superheatSubcoolingStatus")
        or values.get("notesOnRefrigerantStatus")
        or values.get("refrigerantStatus")
        or values.get("refrigerantRange")
    )
    if not refrigerant_condition:
        refrigerant_condition = _merge_conditions(superheat_condition, subcooling_condition)

    carbon_monoxide_condition = _normalize_condition_label(
        values.get("carbonMonoxideStatus")
        or values.get("coStatus")
        or values.get("carbonMonoxideRange")
    )
    if not carbon_monoxide_condition:
        carbon_monoxide_value = _parse_numeric_value(values.get("carbonMonoxide"))
        carbon_monoxide_limit = _parse_numeric_value(
            values.get("carbonMonoxideUpperLimit")
            or values.get("carbonMonoxideMax")
        )
        if carbon_monoxide_limit is None:
            carbon_monoxide_limit = 9.0

        if carbon_monoxide_value is None:
            carbon_monoxide_condition = "No Data"
        elif carbon_monoxide_value < 0:
            carbon_monoxide_condition = "Low"
        elif carbon_monoxide_value > carbon_monoxide_limit:
            carbon_monoxide_condition = "High"
        else:
            carbon_monoxide_condition = "Within Spec"

    return {
        "temperatureDeltaOverallCondition": delta_t_condition,
        "compressorAmperageOverallCondition": compressor_condition,
        "staticPressureOverallCondition": static_pressure_condition,
        "acCapacitorOverallCondition": capacitor_condition,
        "superheatSubcoolingOverallCondition": refrigerant_condition,
        "carbonMonoxideOverallCondition": carbon_monoxide_condition,
    }


def _build_hvac_diagnostics_entry(form_data):
    entry = {}
    for field_name, _label in HVAC_DIAGNOSTIC_FIELDS:
        entry[field_name] = str(form_data.get(field_name, "")).strip()

    entry["date_performed"] = datetime.now().strftime("%m/%d/%Y")
    return entry


def _fetch_latest_hvac_diagnostic(db, hvac_system_id):
    latest = db.hvacDiagnostics.find_one(
        build_reference_filter("hvac_system_id", hvac_system_id),
        sort=[("created_at", -1), ("_id", -1)],
    )
    return serialize_doc(latest) if latest else None


def _fetch_hvac_diagnostics_history(db, hvac_system_id):
    return [
        serialize_doc(entry)
        for entry in db.hvacDiagnostics.find(
            build_reference_filter("hvac_system_id", hvac_system_id)
        ).sort([("created_at", -1), ("_id", -1)])
    ]


def _parse_date_performed(value):
    date_text = str(value or "").strip()
    if not date_text:
        return None

    for date_format in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(date_text, date_format)
        except ValueError:
            continue

    return None


def _sort_diagnostics_by_date_desc(diagnostics):
    if isinstance(diagnostics, dict):
        diagnostics_entries = [diagnostics]
    elif isinstance(diagnostics, list):
        diagnostics_entries = [entry for entry in diagnostics if isinstance(entry, dict)]
    else:
        diagnostics_entries = []

    return sorted(
        diagnostics_entries,
        key=lambda entry: (_parse_date_performed(entry.get("date_performed")) is not None, _parse_date_performed(entry.get("date_performed")) or datetime.min),
        reverse=True,
    )


def _build_latest_diagnostics_card(diagnostics):
    sorted_diagnostics = _sort_diagnostics_by_date_desc(diagnostics)
    latest_diagnostics = sorted_diagnostics[0] if sorted_diagnostics else None

    if not latest_diagnostics:
        return None

    date_performed = str(latest_diagnostics.get("date_performed", "")).strip()
    values = {}
    results = []
    for field_name, field_label in HVAC_DIAGNOSTIC_FIELDS:
        raw_value = latest_diagnostics.get(field_name, "")
        if isinstance(raw_value, (dict, list)):
            value = json.dumps(raw_value)
        else:
            value = str(raw_value).strip()
        values[field_name] = value
        if not value:
            continue
        results.append({"label": field_label, "value": value})

    # Include additional diagnostics keys that may exist in Mongo but are not
    # part of the currently modeled section fields.
    for field_name, raw_value in latest_diagnostics.items():
        if field_name in values or field_name == "date_performed":
            continue
        if isinstance(raw_value, (dict, list)):
            value = json.dumps(raw_value)
        else:
            value = str(raw_value).strip()
        values[field_name] = value

    values.update(_derive_hvac_overall_conditions(values))

    if not date_performed and not results:
        return None

    return {
        "diagnostic_index": 0,
        "date_performed": date_performed or "-",
        "values": values,
        "results": results,
    }


def _build_hvac_diagnostic_detail(diagnostics_entries, diagnostic_index):
    sorted_diagnostics = _sort_diagnostics_by_date_desc(diagnostics_entries)
    if diagnostic_index < 0 or diagnostic_index >= len(sorted_diagnostics):
        return None

    selected_diagnostic = sorted_diagnostics[diagnostic_index]
    date_performed = str(selected_diagnostic.get("date_performed", "")).strip() or "-"
    section_photo_lookup = _build_diagnostic_section_photos_from_entry(selected_diagnostic)
    diagnostic_reports = []
    raw_reports = selected_diagnostic.get("reports")
    if isinstance(raw_reports, list):
        for report in raw_reports:
            if not isinstance(report, dict):
                continue
            report_path = str(report.get("file_path") or "").strip()
            if not report_path:
                continue
            diagnostic_reports.append(
                {
                    "report_number": str(report.get("report_number") or "System Health Report").strip() or "System Health Report",
                    "file_path": report_path,
                    "date_generated": str(report.get("date_generated") or "").strip() or "-",
                }
            )
    section_details = []

    for section_label, fields in HVAC_DIAGNOSTIC_SECTIONS:
        section_key = _diagnostic_section_key(section_label)
        rows = []
        for field in fields:
            field_name = field["name"]
            raw_value = selected_diagnostic.get(field_name, "")
            value = str(raw_value).strip() if raw_value is not None else ""
            rows.append({
                "label": field["label"],
                "value": value or "-",
            })
        section_details.append({
            "label": section_label,
            "key": section_key,
            "rows": rows,
            "photos": section_photo_lookup.get(section_key, []),
        })

    return {
        "diagnostic_id": str(selected_diagnostic.get("_id") or "").strip(),
        "diagnostic_index": diagnostic_index,
        "date_performed": date_performed,
        "sections": section_details,
        "reports": diagnostic_reports,
    }


def _build_hvac_card_component(component, label, collection_name):
    return {
        "label": label,
        "collection_name": collection_name,
        "component_key": collection_name,
        "component_id": str(component.get("_id", "")).strip(),
        "summary": _summarize_hvac_component(component),
        "details": _format_hvac_component_detail(component),
    }


def _build_hvac_ductwork_component(hvac_system):
    ductwork = _extract_hvac_ductwork(hvac_system)
    details = {
        "type": "-",
        "insulated": "-",
        "supply_branches": "-",
        "returns": "-",
        "ductwork_notes": "-",
    }
    summary = "No ductwork details saved yet."

    if ductwork:
        details = {
            "type": ductwork.get("type", "").strip() or "-",
            "insulated": ductwork.get("insulated", "").strip() or "-",
            "supply_branches": ductwork.get("supply_branches", "").strip() or "-",
            "returns": ductwork.get("returns", "").strip() or "-",
            "ductwork_notes": ductwork.get("ductwork_notes", "").strip() or "-",
        }
        summary = _summarize_ductwork(hvac_system) or summary

    return {
        "label": "Ductwork",
        "collection_name": "ductwork",
        "component_key": "ductwork",
        "component_id": "",
        "summary": summary,
        "details": details,
    }


def _build_hvac_component_view_payload(db, customer_id, reference_type, reference_id, component_key):
    if reference_type != "system":
        return None

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customer_id)]})
    if not hvac_system:
        return None

    requested_property_id = str(request.args.get("property_id") or "").strip()
    if requested_property_id:
        hvac_property_id = str(hvac_system.get("property_id") or "").strip()
        if hvac_property_id != requested_property_id:
            return None

    serialized_system = serialize_doc(hvac_system)
    system_type = str(serialized_system.get("system_type", "")).strip()
    allowed_component_keys = _get_allowed_component_keys(system_type)
    if component_key not in allowed_component_keys:
        return None

    latest_diagnostic = _fetch_latest_hvac_diagnostic(db, reference_id)
    diagnostics = _build_latest_diagnostics_card(latest_diagnostic)

    if component_key == "ductwork":
        ductwork = _extract_hvac_ductwork(serialized_system) or {}
        details = {
            "type": str(ductwork.get("type", "")).strip() or "-",
            "insulated": str(ductwork.get("insulated", "")).strip() or "-",
            "supply_branches": str(ductwork.get("supply_branches", "")).strip() or "-",
            "returns": str(ductwork.get("returns", "")).strip() or "-",
            "ductwork_notes": str(ductwork.get("ductwork_notes", "")).strip() or "-",
        }
        component_label = "Ductwork"
    else:
        existing_component = _find_existing_hvac_component(db, customer_id, serialized_system, component_key)
        serialized_component = serialize_doc(existing_component) if existing_component else {}
        details = _format_hvac_component_detail(serialized_component)
        component_label = HVAC_COMPONENT_LABELS.get(component_key, "Component")

    return {
        "reference_type": "system",
        "reference_id": reference_id,
        "system_type": serialized_system.get("system_type", "HVAC System"),
        "component_key": component_key,
        "component_label": component_label,
        "details": details,
        "diagnostics": diagnostics,
    }


def _get_allowed_component_keys(system_type):
    component_keys = [
        collection_name
        for collection_name, _label in HVAC_COLLECTION_CONFIG.get(system_type, ())
    ]
    if system_type in DUCTWORK_SYSTEM_TYPES:
        component_keys.append("ductwork")
    return component_keys


def _load_hvac_components_for_system(db, customer_id, hvac_system):
    system_type = str(hvac_system.get("system_type", "")).strip()
    expected_components = HVAC_COLLECTION_CONFIG.get(system_type, ())
    components = []
    component_snapshots = hvac_system.get("components", {}) if isinstance(hvac_system.get("components"), dict) else {}

    for collection_name, label in expected_components:
        snapshot = component_snapshots.get(collection_name)
        if isinstance(snapshot, dict) and snapshot:
            components.append(_build_hvac_card_component(snapshot, label, collection_name))

    return components


def _build_hvac_detail_payload(db, customer_id, reference_type, reference_id):
    if reference_type != "system":
        return None

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customer_id)]})
    if not hvac_system:
        return None

    requested_property_id = str(request.args.get("property_id") or "").strip()
    if requested_property_id:
        hvac_property_id = str(hvac_system.get("property_id") or "").strip()
        if hvac_property_id != requested_property_id:
            return None

    serialized_system = serialize_doc(hvac_system)
    components = _load_hvac_components_for_system(db, customer_id, serialized_system)
    latest_diagnostic = _fetch_latest_hvac_diagnostic(db, reference_id)
    diagnostics = _build_latest_diagnostics_card(latest_diagnostic)
    reports = []
    for report in serialized_system.get("reports", []):
        if not isinstance(report, dict):
            continue
        reports.append(
            {
                "report_number": str(report.get("report_number", "System Health Report")).strip() or "System Health Report",
                "file_path": str(report.get("file_path", "#")).strip() or "#",
                "date_generated": str(report.get("date_generated", "")).strip() or "-",
                "diagnostics_date_performed": str(report.get("diagnostics_date_performed", "")).strip() or "-",
            }
        )

    if serialized_system.get("system_type") in DUCTWORK_SYSTEM_TYPES:
        components.append(_build_hvac_ductwork_component(serialized_system))

    return {
        "reference_type": "system",
        "reference_id": reference_id,
        "title": f"{serialized_system.get('system_type', 'HVAC System')}",
        "system_type": serialized_system.get("system_type", "HVAC System"),
        "system_tonnage": str(serialized_system.get("system_tonnage", "")).strip() or "-",
        "cooling_capacity": str(serialized_system.get("cooling_capacity", "")).strip() or "-",
        "heating_capacity": str(serialized_system.get("heating_capacity", "")).strip() or "-",
        "property_id": str(serialized_system.get("property_id", "")).strip() or "-",
        "components": components,
        "diagnostics": diagnostics,
        "reports": reports,
        "photos": [
            {
                "url": url_for("static", filename=f"{HVAC_PHOTO_UPLOAD_SUBDIR.replace(os.sep, '/')}/{photo.get('filename', '')}"),
                "filename": str(photo.get("filename", "")).strip(),
                "uploaded_at": str(photo.get("uploaded_at", "")).strip() or "-",
            }
            for photo in serialized_system.get("photos", [])
            if isinstance(photo, dict) and photo.get("filename")
        ],
    }


def _build_hvac_system_cards(db, customer_id, property_id=None):
    base_systems = [
        serialize_doc(hvac_system)
        for hvac_system in db.hvacSystems.find(build_reference_filter("customer_id", customer_id)).sort([("_id", -1)])
    ]

    normalized_property_id = str(property_id or "").strip()
    if normalized_property_id:
        filtered_base_systems = []
        for base_system in base_systems:
            system_property_id = str(base_system.get("property_id") or "").strip()
            if system_property_id == normalized_property_id:
                filtered_base_systems.append(base_system)
        base_systems = filtered_base_systems

    hvac_cards = []

    for base_system in base_systems:
        system_type = str(base_system.get("system_type", "")).strip()
        loaded_components = _load_hvac_components_for_system(db, customer_id, base_system)
        card_ductwork_summary = _summarize_ductwork(base_system)

        hvac_cards.append(
            {
                "reference_type": "system",
                "reference_id": str(base_system.get("_id", "")).strip(),
                "system_type": system_type or "HVAC System",
                "system_tonnage": str(base_system.get("system_tonnage", "")).strip() or "-",
                "cooling_capacity": str(base_system.get("cooling_capacity", "")).strip() or "-",
                "heating_capacity": str(base_system.get("heating_capacity", "")).strip() or "-",
                "ductwork_summary": card_ductwork_summary,
                "components": [
                    {
                        "label": component["label"],
                        "summary": component["summary"],
                    }
                    for component in loaded_components
                ],
            }
        )

    return hvac_cards


@bp.route("/customers")
def customers():
    db = ensure_connection_or_500()
    customers_list = [
        serialize_doc(customer)
        for customer in db.customers.find().sort([("last_name", 1), ("first_name", 1)])
    ]
    return render_template("customers/customers.html", customers=customers_list)


@bp.route("/customers/export/csv")
def export_customers_csv():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)

    query = {"_id": None}
    if business_id:
        related_customer_ids = set()

        for raw_customer_id in db.jobs.distinct("customer_id", {"business_id": business_id}):
            if isinstance(raw_customer_id, ObjectId):
                related_customer_ids.add(raw_customer_id)
            elif isinstance(raw_customer_id, str) and ObjectId.is_valid(raw_customer_id):
                related_customer_ids.add(ObjectId(raw_customer_id))

        for raw_customer_id in db.estimates.distinct("customer_id", {"business_id": business_id}):
            if isinstance(raw_customer_id, ObjectId):
                related_customer_ids.add(raw_customer_id)
            elif isinstance(raw_customer_id, str) and ObjectId.is_valid(raw_customer_id):
                related_customer_ids.add(ObjectId(raw_customer_id))

        business_scoped_filters = [
            {"business": business_id},
            {"business": str(business_id)},
            {"business_id": business_id},
            {"business_id": str(business_id)},
        ]
        if related_customer_ids:
            business_scoped_filters.append({"_id": {"$in": list(related_customer_ids)}})

        query = {"$or": business_scoped_filters}

    customers_rows = list(db.customers.find(query).sort([("last_name", 1), ("first_name", 1)]))
    return build_csv_export_response(customers_rows, "customers_export.csv")


@bp.route("/customers/add", methods=["GET", "POST"])
def add_customer():
    db = ensure_connection_or_500()
    if request.method == "POST":
        form_data = request.form.to_dict()
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        company = request.form.get("company", "").strip()
        customer_type = _normalize_customer_type(request.form.get("customer_type", "Residential"))
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        address_line_1 = request.form.get("address_line_1", "").strip()
        address_line_2 = request.form.get("address_line_2", "").strip()
        city = request.form.get("city", "").strip()
        state = request.form.get("state", "").strip().upper()
        zip_code = request.form.get("zip_code", "").strip()
        referral_source = request.form.get("referral_source", "").strip()

        if not first_name or not last_name:
            return render_template(
                "customers/add_customer.html",
                error="First name and last name are required.",
                form_data=form_data,
            )

        if email and not _email_is_valid(email):
            return render_template(
                "customers/add_customer.html",
                error=EMAIL_VALIDATION_MESSAGE,
                form_data=form_data,
            )

        customer_status = "Active" if all((phone, email, address_line_1, city, state)) else "Lead"

        customer_count = db.customers.count_documents({}) + 1
        customer = {
            "first_name": first_name,
            "last_name": last_name,
            "company": company,
            "customer_type": customer_type,
            "phone": phone,
            "email": email,
            "address_line_1": address_line_1,
            "address_line_2": address_line_2,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "properties": [
                {
                    "property_id": str(ObjectId()),
                    "property_name": "Primary Residence",
                    "property_type": "single_family_home",
                    "address_line_1": address_line_1,
                    "address_line_2": address_line_2,
                    "city": city,
                    "state": state,
                    "zip_code": zip_code,
                    "is_default": True,
                    "is_seed_primary_address": True,
                }
            ],
            "referral_source": referral_source,
            "customer_status": customer_status,
            "date_added": datetime.now().strftime("%m/%d/%Y"),
            "created_at": datetime.utcnow(),
            "account_number": f"ACC-{customer_count:05d}",
            "account_type": "Residential",
            "balance_due": "$0.00",
            "balance_due_amount": 0.0,
            "account_status": "Current",
        }
        inserted = db.customers.insert_one(customer)
        current_app.logger.info("Customer created: id=%s by employee_id=%s", str(inserted.inserted_id), session.get("employee_id"))
        return redirect(url_for("customers.view_customer", customerId=str(inserted.inserted_id)))

    return render_template("customers/add_customer.html", error="", form_data={})


@bp.route("/customers/<customerId>/update", methods=["GET", "POST"])
def update_customer(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        address_line_1 = request.form.get("address_line_1", "").strip()
        city = request.form.get("city", "").strip()
        state = request.form.get("state", "").strip().upper()
        zip_code = request.form.get("zip_code", "").strip()

        if not first_name or not last_name:
            return render_template(
                "customers/update_customer.html",
                customerId=customerId,
                customer=serialize_doc(customer),
                error="First name and last name are required.",
            )

        current_status = str(customer.get("customer_status", "")).strip()
        next_status = current_status
        if current_status.lower() == "lead" and all((phone, email, address_line_1, city, state)):
            next_status = "Active"

        update_data = {
            "first_name": first_name,
            "last_name": last_name,
            "company": request.form.get("company", "").strip(),
            "phone": phone,
            "email": email,
            "address_line_1": address_line_1,
            "address_line_2": request.form.get("address_line_2", "").strip(),
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "referral_source": request.form.get("referral_source", "").strip(),
            "customer_status": next_status,
        }

        address_fields = ("address_line_1", "address_line_2", "city", "state", "zip_code")
        address_changed = any(
            str(customer.get(field, "") or "").strip() != str(update_data.get(field, "") or "").strip()
            for field in address_fields
        )

        if address_changed:
            customer_properties = _get_customer_properties(customer)
            synced_properties = []
            for prop in customer_properties:
                synced_prop = dict(prop)
                if synced_prop.get("is_seed_primary_address"):
                    synced_prop["address_line_1"] = update_data["address_line_1"]
                    synced_prop["address_line_2"] = update_data["address_line_2"]
                    synced_prop["city"] = update_data["city"]
                    synced_prop["state"] = update_data["state"]
                    synced_prop["zip_code"] = update_data["zip_code"]
                synced_properties.append(synced_prop)
            update_data["properties"] = synced_properties

        db.customers.update_one({"_id": ObjectId(customerId)}, {"$set": update_data})
        return redirect(url_for("customers.view_customer", customerId=customerId))

    return render_template(
        "customers/update_customer.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        error="",
    )


@bp.route("/customers/<customerId>/delete", methods=["POST"])
def delete_customer(customerId):
    db = ensure_connection_or_500()
    customer_oid = object_id_or_404(customerId)
    customer = db.customers.find_one({"_id": customer_oid})
    if not customer:
        return redirect(url_for("customers.customers"))

    related_jobs = list(db.jobs.find(build_reference_filter("customer_id", customerId), {"_id": 1}))
    related_job_ids = [str(job.get("_id")) for job in related_jobs]

    db.customers.delete_one({"_id": customer_oid})
    db.jobs.delete_many(build_reference_filter("customer_id", customerId))
    db.equipment.delete_many(build_reference_filter("customer_id", customerId))
    db.estimates.delete_many(build_reference_filter("customer_id", customerId))
    if related_job_ids:
        db.estimates.delete_many({"job_id": {"$in": related_job_ids}})
    current_app.logger.info("Customer deleted: id=%s by employee_id=%s", customerId, session.get("employee_id"))
    return redirect(url_for("customers.customers"))


@bp.route("/customers/<customerId>/properties/add", methods=["GET", "POST"])
def add_property(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    customer_type = _normalize_customer_type(customer.get("customer_type", "Residential"))
    property_type_options = _get_property_type_options(customer_type)
    form_data = {
        "property_id": str(ObjectId()),
        "property_name": "",
        "property_type": property_type_options[0] if property_type_options else "",
        "property_type_other": "",
        "address_line_1": "",
        "address_line_2": "",
        "city": "",
        "state": "",
        "zip_code": "",
        "is_default": False,
        "is_seed_primary_address": False,
    }
    error = ""

    if request.method == "POST":
        submitted = request.form.to_dict()
        submitted["property_id"] = form_data["property_id"]
        form_data = _normalize_property_payload(submitted, customer_type)

        if not _property_payload_is_valid(form_data):
            error = "Property name, property type, address line 1, city, state, and zip code are required."
        else:
            customer_properties = _get_customer_properties(customer)
            form_data["is_default"] = False
            form_data["is_seed_primary_address"] = False
            customer_properties.append(form_data)
            db.customers.update_one(
                {"_id": ObjectId(customerId)},
                {"$set": {"properties": customer_properties}},
            )
            return redirect(url_for("customers.view_customer", customerId=customerId))

    return render_template(
        "customers/add_property.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        customer_type=customer_type,
        property_type_options=property_type_options,
        property_type_labels=PROPERTY_TYPE_LABELS,
        form_data=form_data,
        error=error,
    )


@bp.route("/customers/<customerId>/properties/<propertyId>")
def view_property(customerId, propertyId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    customer_property = _find_customer_property(customer, propertyId)
    if not customer_property:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_systems = _build_hvac_system_cards(db, customerId, propertyId)

    return render_template(
        "customers/view_property.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        property=customer_property,
        propertyId=propertyId,
        hvac_systems=hvac_systems,
    )


@bp.route("/customers/<customerId>/properties/<propertyId>/update", methods=["GET", "POST"])
def update_property(customerId, propertyId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    existing_property = _find_customer_property(customer, propertyId)
    if not existing_property:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    customer_type = _normalize_customer_type(customer.get("customer_type", "Residential"))
    property_type_options = _get_property_type_options(customer_type)
    raw_property_type = str(existing_property.get("property_type") or "").strip()
    form_data = dict(existing_property)
    if raw_property_type not in property_type_options:
        form_data["property_type"] = "other"
        form_data["property_type_other"] = raw_property_type
    else:
        form_data["property_type"] = raw_property_type
        form_data["property_type_other"] = str(existing_property.get("property_type_other") or "").strip()

    error = ""
    if request.method == "POST":
        submitted = request.form.to_dict()
        submitted["property_id"] = propertyId
        form_data = _normalize_property_payload(submitted, customer_type)
        form_data["is_default"] = bool(existing_property.get("is_default"))
        form_data["is_seed_primary_address"] = bool(existing_property.get("is_seed_primary_address"))

        if not _property_payload_is_valid(form_data):
            error = "Property name, property type, address line 1, city, state, and zip code are required."
        else:
            customer_properties = []
            for prop in _get_customer_properties(customer):
                if prop.get("property_id") == propertyId:
                    customer_properties.append(form_data)
                else:
                    customer_properties.append(prop)

            db.customers.update_one(
                {"_id": ObjectId(customerId)},
                {"$set": {"properties": customer_properties}},
            )
            return redirect(url_for("customers.view_property", customerId=customerId, propertyId=propertyId))

    return render_template(
        "customers/update_property.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        property=existing_property,
        propertyId=propertyId,
        customer_type=customer_type,
        property_type_options=property_type_options,
        property_type_labels=PROPERTY_TYPE_LABELS,
        form_data=form_data,
        error=error,
    )


@bp.route("/customers/<customerId>/properties/<propertyId>/delete", methods=["POST"])
def delete_property(customerId, propertyId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    customer_properties = [
        prop
        for prop in _get_customer_properties(customer)
        if prop.get("property_id") != propertyId
    ]
    db.customers.update_one(
        {"_id": ObjectId(customerId)},
        {"$set": {"properties": customer_properties}},
    )

    db.hvacSystems.delete_many(
        {
            "$and": [
                build_reference_filter("customer_id", customerId),
                build_reference_filter("property_id", propertyId),
            ]
        }
    )

    return redirect(url_for("customers.view_customer", customerId=customerId))


@bp.route("/customers/<customerId>/properties/<propertyId>/default", methods=["POST"])
def set_default_property(customerId, propertyId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    customer_properties = _get_customer_properties(customer)
    property_exists = any(prop.get("property_id") == propertyId for prop in customer_properties)
    if not property_exists:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    updated_properties = []
    for prop in customer_properties:
        updated_prop = dict(prop)
        updated_prop["is_default"] = prop.get("property_id") == propertyId
        updated_properties.append(updated_prop)

    db.customers.update_one(
        {"_id": ObjectId(customerId)},
        {"$set": {"properties": updated_properties}},
    )

    return redirect(url_for("customers.view_customer", customerId=customerId))


@bp.route("/customers/<customerId>")
def view_customer(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    jobs_page_raw = request.args.get("jobs_page", "1")
    payments_page_raw = request.args.get("payments_page", "1")
    estimates_page_raw = request.args.get("estimates_page", "1")

    try:
        jobs_page = max(1, int(jobs_page_raw))
    except ValueError:
        jobs_page = 1

    try:
        payments_page = max(1, int(payments_page_raw))
    except ValueError:
        payments_page = 1

    try:
        estimates_page = max(1, int(estimates_page_raw))
    except ValueError:
        estimates_page = 1

    jobs_per_page = 5
    payments_per_page = 5
    estimates_per_page = 5

    customer_jobs_total = db.jobs.count_documents(build_reference_filter("customer_id", customerId))
    customer_jobs_total_pages = (customer_jobs_total + jobs_per_page - 1) // jobs_per_page
    if customer_jobs_total_pages == 0:
        jobs_page = 1
    elif jobs_page > customer_jobs_total_pages:
        jobs_page = customer_jobs_total_pages

    customer_payments_total = db.payments.count_documents(build_reference_filter("customer_id", customerId))
    customer_payments_total_pages = (customer_payments_total + payments_per_page - 1) // payments_per_page
    if customer_payments_total_pages == 0:
        payments_page = 1
    elif payments_page > customer_payments_total_pages:
        payments_page = customer_payments_total_pages

    customer_estimates_total = db.estimates.count_documents(build_reference_filter("customer_id", customerId))
    customer_estimates_total_pages = (customer_estimates_total + estimates_per_page - 1) // estimates_per_page
    if customer_estimates_total_pages == 0:
        estimates_page = 1
    elif estimates_page > customer_estimates_total_pages:
        estimates_page = customer_estimates_total_pages

    customer_pages = {
        "jobs": jobs_page,
        "payments": payments_page,
        "estimates": estimates_page,
    }

    jobs_skip = (jobs_page - 1) * jobs_per_page
    customer_jobs = [
        serialize_doc(job)
        for job in db.jobs.find(build_reference_filter("customer_id", customerId)).sort([("scheduled_date", -1), ("scheduled_time", -1)]).skip(jobs_skip).limit(jobs_per_page)
    ]

    payments_skip = (payments_page - 1) * payments_per_page
    customer_payments = [
        serialize_doc(payment)
        for payment in db.payments.find(build_reference_filter("customer_id", customerId)).sort([("date", -1), ("_id", -1)]).skip(payments_skip).limit(payments_per_page)
    ]

    estimates_skip = (estimates_page - 1) * estimates_per_page
    customer_estimates = [
        serialize_doc(estimate)
        for estimate in db.estimates.find(build_reference_filter("customer_id", customerId)).sort([("_id", -1)]).skip(estimates_skip).limit(estimates_per_page)
    ]

    for estimate in customer_estimates:
        estimate["status"] = str(estimate.get("status") or "Created")

    properties = _get_customer_properties(customer)

    return render_template(
        "customers/view_customer.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        customer_pages=customer_pages,
        customer_jobs=customer_jobs,
        customer_jobs_total_pages=customer_jobs_total_pages,
        customer_payments=customer_payments,
        customer_payments_total_pages=customer_payments_total_pages,
        customer_estimates=customer_estimates,
        customer_estimates_total_pages=customer_estimates_total_pages,
        properties=properties,
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>")
def view_hvac_system(customerId, reference_type, reference_id):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    hvac_system = _build_hvac_detail_payload(db, customerId, reference_type, reference_id)
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    property_id = str(request.args.get("property_id") or "").strip()

    return render_template(
        "equipment/view_hvac_system.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        hvac_system=hvac_system,
        property_id=property_id,
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/diagnostics")
def view_hvac_diagnostics(customerId, reference_type, reference_id):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))
    property_id = str(request.args.get("property_id") or "").strip()
    if reference_type != "system":
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    if property_id:
        if str(hvac_system.get("property_id") or "").strip() != property_id:
            return redirect(url_for("customers.view_customer", customerId=customerId))

    serialized_system = serialize_doc(hvac_system)
    diagnostics_entries = _sort_diagnostics_by_date_desc(_fetch_hvac_diagnostics_history(db, reference_id))
    if not diagnostics_entries:
        return redirect(
            url_for(
                "customers.view_hvac_system",
                customerId=customerId,
                reference_type=reference_type,
                reference_id=reference_id,
                property_id=property_id,
            )
        )

    return render_template(
        "equipment/view_hvac_diagnostic.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        reference_type=reference_type,
        reference_id=reference_id,
        hvac_system=serialized_system,
        diagnostics=[
            {
                "diagnostic_index": diagnostic_index,
                "date_performed": str(entry.get("date_performed", "")).strip() or "-",
                "system_type": serialized_system.get("system_type", "HVAC System"),
                "result_count": sum(
                    1
                    for field_name, _field_label in HVAC_DIAGNOSTIC_FIELDS
                    if str(entry.get(field_name, "")).strip()
                ),
            }
            for diagnostic_index, entry in enumerate(diagnostics_entries)
        ],
        latest_date_performed=str(diagnostics_entries[0].get("date_performed", "")).strip() or "-",
        property_id=property_id,
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/diagnostics/<int:diagnostic_index>")
def view_hvac_diagnostic(customerId, reference_type, reference_id, diagnostic_index):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))
    property_id = str(request.args.get("property_id") or "").strip()
    if reference_type != "system":
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    if property_id:
        if str(hvac_system.get("property_id") or "").strip() != property_id:
            return redirect(url_for("customers.view_customer", customerId=customerId))

    serialized_system = serialize_doc(hvac_system)
    diagnostics_entries = _sort_diagnostics_by_date_desc(_fetch_hvac_diagnostics_history(db, reference_id))
    diagnostic_detail = _build_hvac_diagnostic_detail(diagnostics_entries, diagnostic_index)
    if not diagnostic_detail:
        return redirect(
            url_for(
                "customers.view_hvac_diagnostics",
                customerId=customerId,
                reference_type=reference_type,
                reference_id=reference_id,
                property_id=property_id,
            )
        )

    report_email_template = ""
    employee_id = session.get("employee_id")
    if employee_id and ObjectId.is_valid(employee_id):
        employee = db.employees.find_one({"_id": ObjectId(employee_id)})
        if employee:
            business_ref = employee.get("business")
            business_oid = None
            if isinstance(business_ref, ObjectId):
                business_oid = business_ref
            elif isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
                business_oid = ObjectId(business_ref)
            if business_oid:
                business = db.businesses.find_one({"_id": business_oid})
                if business:
                    report_email_template = str(business.get("report_email_template") or "").strip()

    latest_report_file = diagnostic_detail.get("reports", [{}])[0].get("file_path", "") if diagnostic_detail.get("reports") else ""

    return render_template(
        "equipment/view_hvac_diagnostic_detail.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        reference_type=reference_type,
        reference_id=reference_id,
        hvac_system=serialized_system,
        diagnostic=diagnostic_detail,
        latest_report_file=latest_report_file,
        report_email_template=report_email_template,
        property_id=property_id,
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/diagnostics/add", methods=["GET", "POST"])
def add_hvac_diagnostics(customerId, reference_type, reference_id):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))
    property_id = str(request.args.get("property_id") or "").strip()
    if reference_type != "system":
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    if property_id:
        if str(hvac_system.get("property_id") or "").strip() != property_id:
            return redirect(url_for("customers.view_customer", customerId=customerId))

    error = ""
    form_data = {
        field_name: ""
        for field_name, _label in HVAC_DIAGNOSTIC_FIELDS
    }

    if request.method == "POST":
        form_data = {
            field_name: request.form.get(field_name, "").strip()
            for field_name, _label in HVAC_DIAGNOSTIC_FIELDS
        }

        upload_dir = os.path.join(current_app.root_path, "static", HVAC_PHOTO_UPLOAD_SUBDIR)
        os.makedirs(upload_dir, exist_ok=True)

        section_photos = {}
        saved_file_paths = []
        for section_label, _fields in HVAC_DIAGNOSTIC_SECTIONS:
            section_key = _diagnostic_section_key(section_label)
            photo_files = request.files.getlist(f"section_photos__{section_key}[]")
            photo_captions = request.form.getlist(f"section_photo_captions__{section_key}[]")
            photo_files = [f for f in photo_files if getattr(f, "filename", "")]
            if not photo_files:
                continue

            saved_photos = []
            for photo_index, photo_file in enumerate(photo_files):
                original_name = str(getattr(photo_file, "filename", "") or "").strip()
                if not original_name:
                    continue

                extension = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
                if extension not in ALLOWED_PHOTO_EXTENSIONS:
                    error = (
                        f"Unsupported file type for {section_label} photos. "
                        "Please upload JPG, JPEG, PNG, WEBP, or HEIC."
                    )
                    break

                photo_file.seek(0, 2)
                file_size = photo_file.tell()
                photo_file.seek(0)
                if file_size > MAX_PHOTO_FILE_SIZE:
                    error = f"A {section_label} photo exceeded the 10 MB size limit."
                    break

                safe_name = (
                    f"diag_{reference_id}_{section_key}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}_"
                    f"{secure_filename(original_name)}"
                )
                save_path = os.path.join(upload_dir, safe_name)
                try:
                    photo_file.save(save_path)
                    saved_file_paths.append(save_path)
                except Exception:
                    error = f"Failed to upload a photo for {section_label}. Please try again."
                    break

                caption = ""
                if photo_index < len(photo_captions):
                    caption = str(photo_captions[photo_index] or "").strip()
                if len(caption) > MAX_PHOTO_CAPTION_LENGTH:
                    caption = caption[:MAX_PHOTO_CAPTION_LENGTH]

                saved_photos.append(
                    {
                        "filename": safe_name,
                        "url": _build_hvac_photo_url(safe_name),
                        "caption": caption,
                        "uploaded_at": datetime.now().strftime("%m/%d/%Y"),
                    }
                )

            if error:
                break

            if saved_photos:
                section_photos[section_key] = saved_photos

        if error:
            for saved_file_path in saved_file_paths:
                try:
                    if os.path.exists(saved_file_path):
                        os.remove(saved_file_path)
                except Exception:
                    pass
            return render_template(
                "equipment/add_hvac_diagnostics.html",
                customerId=customerId,
                customer=serialize_doc(customer),
                reference_type=reference_type,
                reference_id=reference_id,
                hvac_system=serialize_doc(hvac_system),
                error=error,
                form_data=form_data,
                diagnostics_sections=HVAC_DIAGNOSTIC_SECTIONS,
                property_id=property_id,
            )

        diagnostics_entry = _build_hvac_diagnostics_entry(form_data)
        diagnostics_entry.update(
            {
                "hvac_system_id": reference_value(reference_id),
                "customer_id": reference_value(customerId),
                "property_id": reference_value(property_id) if property_id else None,
                "section_photos": section_photos,
                "created_at": datetime.utcnow(),
            }
        )

        db.hvacDiagnostics.insert_one(diagnostics_entry)

        return redirect(
            url_for(
                "customers.view_hvac_system",
                customerId=customerId,
                reference_type=reference_type,
                reference_id=reference_id,
                property_id=property_id,
            )
        )

    return render_template(
        "equipment/add_hvac_diagnostics.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        reference_type=reference_type,
        reference_id=reference_id,
        hvac_system=serialize_doc(hvac_system),
        error=error,
        form_data=form_data,
        diagnostics_sections=HVAC_DIAGNOSTIC_SECTIONS,
        property_id=property_id,
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/diagnostics/<diagnosticId>/photos/caption", methods=["POST"])
def update_hvac_diagnostic_photo_caption(customerId, reference_type, reference_id, diagnosticId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))
    property_id = str(request.args.get("property_id") or "").strip()
    if reference_type != "system":
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))
    if property_id and str(hvac_system.get("property_id") or "").strip() != property_id:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    diagnostic = db.hvacDiagnostics.find_one(
        {
            "$and": [
                {"_id": object_id_or_404(diagnosticId)},
                build_reference_filter("customer_id", customerId),
                build_reference_filter("hvac_system_id", reference_id),
            ]
        }
    )
    if not diagnostic:
        return redirect(
            url_for(
                "customers.view_hvac_diagnostics",
                customerId=customerId,
                reference_type=reference_type,
                reference_id=reference_id,
                property_id=property_id,
            )
        )

    section_key = _diagnostic_section_key(request.form.get("section_key", ""))
    filename = str(request.form.get("filename", "")).strip()
    new_caption = str(request.form.get("caption", "")).strip()
    if len(new_caption) > MAX_PHOTO_CAPTION_LENGTH:
        new_caption = new_caption[:MAX_PHOTO_CAPTION_LENGTH]

    section_photos, resolved_key, photo_list = _resolve_section_photo_bucket(diagnostic.get("section_photos"), section_key)
    if filename:
        for photo in photo_list:
            if not isinstance(photo, dict):
                continue
            if str(photo.get("filename") or "").strip() == filename:
                photo["caption"] = new_caption
                break

        db.hvacDiagnostics.update_one(
            {"_id": diagnostic["_id"]},
            {
                "$set": {
                    "section_photos": section_photos,
                    "updated_at": datetime.utcnow(),
                }
            },
        )

    diagnostic_index_raw = str(request.form.get("diagnostic_index") or "0").strip()
    try:
        diagnostic_index = max(0, int(diagnostic_index_raw))
    except ValueError:
        diagnostic_index = 0

    return redirect(
        url_for(
            "customers.view_hvac_diagnostic",
            customerId=customerId,
            reference_type=reference_type,
            reference_id=reference_id,
            diagnostic_index=diagnostic_index,
            property_id=property_id,
        )
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/diagnostics/<diagnosticId>/photos/delete", methods=["POST"])
def delete_hvac_diagnostic_photo(customerId, reference_type, reference_id, diagnosticId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))
    property_id = str(request.args.get("property_id") or "").strip()
    if reference_type != "system":
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))
    if property_id and str(hvac_system.get("property_id") or "").strip() != property_id:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    diagnostic = db.hvacDiagnostics.find_one(
        {
            "$and": [
                {"_id": object_id_or_404(diagnosticId)},
                build_reference_filter("customer_id", customerId),
                build_reference_filter("hvac_system_id", reference_id),
            ]
        }
    )
    if not diagnostic:
        return redirect(
            url_for(
                "customers.view_hvac_diagnostics",
                customerId=customerId,
                reference_type=reference_type,
                reference_id=reference_id,
                property_id=property_id,
            )
        )

    section_key = _diagnostic_section_key(request.form.get("section_key", ""))
    filename = str(request.form.get("filename", "")).strip()
    section_photos, resolved_key, photo_list = _resolve_section_photo_bucket(diagnostic.get("section_photos"), section_key)

    if filename:
        updated_photo_list = []
        for photo in photo_list:
            if not isinstance(photo, dict):
                continue
            if str(photo.get("filename") or "").strip() == filename:
                continue
            updated_photo_list.append(photo)

        section_photos[resolved_key] = updated_photo_list
        db.hvacDiagnostics.update_one(
            {"_id": diagnostic["_id"]},
            {
                "$set": {
                    "section_photos": section_photos,
                    "updated_at": datetime.utcnow(),
                }
            },
        )

        file_path = os.path.join(current_app.root_path, "static", HVAC_PHOTO_UPLOAD_SUBDIR, filename)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

    diagnostic_index_raw = str(request.form.get("diagnostic_index") or "0").strip()
    try:
        diagnostic_index = max(0, int(diagnostic_index_raw))
    except ValueError:
        diagnostic_index = 0

    return redirect(
        url_for(
            "customers.view_hvac_diagnostic",
            customerId=customerId,
            reference_type=reference_type,
            reference_id=reference_id,
            diagnostic_index=diagnostic_index,
            property_id=property_id,
        )
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/reports/generate", methods=["POST"])
def generate_hvac_system_report(customerId, reference_type, reference_id):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))
    property_id = str(request.args.get("property_id") or "").strip()
    if reference_type != "system":
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    if property_id:
        if str(hvac_system.get("property_id") or "").strip() != property_id:
            return redirect(url_for("customers.view_customer", customerId=customerId))

    serialized_system = serialize_doc(hvac_system)
    diagnostics_entries = _sort_diagnostics_by_date_desc(_fetch_hvac_diagnostics_history(db, reference_id))
    diagnostic_index_raw = request.form.get("diagnostic_index", "").strip()
    selected_index = 0
    if diagnostic_index_raw:
        try:
            selected_index = max(0, int(diagnostic_index_raw))
        except ValueError:
            selected_index = 0

    raw_diagnostics = diagnostics_entries[selected_index] if selected_index < len(diagnostics_entries) else None
    diagnostics_card = _build_latest_diagnostics_card(raw_diagnostics)
    if not diagnostics_card:
        return redirect(
            url_for(
                "customers.view_hvac_diagnostics",
                customerId=customerId,
                reference_type=reference_type,
                reference_id=reference_id,
                property_id=property_id,
            )
        )

    selected_diagnostic_id = str(raw_diagnostics.get("_id") or "").strip()
    if not ObjectId.is_valid(selected_diagnostic_id):
        return redirect(
            url_for(
                "customers.view_hvac_diagnostics",
                customerId=customerId,
                reference_type=reference_type,
                reference_id=reference_id,
                property_id=property_id,
            )
        )

    diagnostic_doc = db.hvacDiagnostics.find_one(
        {
            "$and": [
                {"_id": ObjectId(selected_diagnostic_id)},
                build_reference_filter("customer_id", customerId),
                build_reference_filter("hvac_system_id", reference_id),
            ]
        }
    )
    if not diagnostic_doc:
        return redirect(
            url_for(
                "customers.view_hvac_diagnostics",
                customerId=customerId,
                reference_type=reference_type,
                reference_id=reference_id,
                property_id=property_id,
            )
        )

    existing_reports = diagnostic_doc.get("reports", [])
    reports_history = existing_reports if isinstance(existing_reports, list) else []
    report_number = f"RPT-{reference_id[:8].upper()}-{len(reports_history) + 1:02d}"

    report_path = generate_hvac_system_health_report(
        hvac_system_id=reference_id,
        customer=serialize_doc(customer),
        hvac_system=serialized_system,
        diagnostics_card=diagnostics_card,
        report_number=report_number,
        raw_diagnostics=raw_diagnostics,
        business=serialize_doc(db.businesses.find_one({"_id": _resolve_current_business_id(db)}) or {}),
    )
    filename = os.path.basename(report_path)
    report_item = {
        "report_number": report_number,
        "file_path": url_for("download_invoice", filename=filename),
        "date_generated": datetime.now().strftime("%m/%d/%Y"),
        "diagnostics_date_performed": diagnostics_card.get("date_performed", "-"),
    }

    db.hvacDiagnostics.update_one(
        {"_id": diagnostic_doc["_id"]},
        {
            "$set": {
                "reports": [report_item, *reports_history],
                "updated_at": datetime.utcnow(),
            }
        },
    )

    return redirect(
        url_for(
            "customers.view_hvac_diagnostic",
            customerId=customerId,
            reference_type=reference_type,
            reference_id=reference_id,
            diagnostic_index=selected_index,
            property_id=property_id,
        )
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/diagnostics/<int:diagnostic_index>/email", methods=["POST"])
def send_hvac_report_email(customerId, reference_type, reference_id, diagnostic_index):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return jsonify({"success": False, "error": "Customer not found"}), 404

    property_id = str(request.args.get("property_id") or "").strip()
    if reference_type != "system":
        return jsonify({"success": False, "error": "Unsupported reference type"}), 400

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return jsonify({"success": False, "error": "HVAC system not found"}), 404
    if property_id and str(hvac_system.get("property_id") or "").strip() != property_id:
        return jsonify({"success": False, "error": "Invalid property"}), 403

    diagnostics_entries = _sort_diagnostics_by_date_desc(_fetch_hvac_diagnostics_history(db, reference_id))
    if diagnostic_index < 0 or diagnostic_index >= len(diagnostics_entries):
        return jsonify({"success": False, "error": "Diagnostic not found"}), 404

    selected_diagnostic = diagnostics_entries[diagnostic_index]

    try:
        data = request.get_json() or {}
        recipient_email = str(data.get("recipient_email") or "").strip()
        subject = str(data.get("subject") or "").strip()
        body = str(data.get("body") or "").strip()
        report_file = str(data.get("estimate_file") or "").strip()

        if not recipient_email or not subject or not body or not report_file:
            return jsonify({"success": False, "error": "Missing required fields"}), 400

        filename = report_file.split("/")[-1]
        base_dir = os.path.dirname(os.path.dirname(__file__))
        invoices_dir = os.path.join(base_dir, "invoices")
        filepath = os.path.join(invoices_dir, filename)

        if not os.path.exists(filepath) or not os.path.abspath(filepath).startswith(os.path.abspath(invoices_dir)):
            return jsonify({"success": False, "error": "Report file not found"}), 404

        msg = Message(
            subject=subject,
            recipients=[recipient_email],
            body=body,
        )

        with open(filepath, "rb") as report_fp:
            msg.attach(filename, "application/pdf", report_fp.read())

        current_app.extensions["mail"].send(msg)

        db.hvacDiagnostics.update_one(
            {"_id": object_id_or_404(str(selected_diagnostic.get("_id") or ""))},
            {
                "$set": {
                    "report_email_last_sent_at": datetime.utcnow(),
                    "report_email_last_recipient": recipient_email,
                }
            },
        )
        return jsonify({"success": True}), 200
    except Exception as exc:
        current_app.logger.error("HVAC report email send failed: customer_id=%s reference_id=%s diagnostic_index=%s error=%s", customerId, reference_id, diagnostic_index, exc)
        return jsonify({"success": False, "error": str(exc)}), 500


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/components/<component_key>")
def view_hvac_component(customerId, reference_type, reference_id, component_key):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    component = _build_hvac_component_view_payload(db, customerId, reference_type, reference_id, component_key)
    if not component:
        return redirect(
            url_for(
                "customers.view_hvac_system",
                customerId=customerId,
                reference_type=reference_type,
                reference_id=reference_id,
            )
        )

    property_id = str(request.args.get("property_id") or "").strip()

    return render_template(
        "equipment/view_hvac_component.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        component=component,
        property_id=property_id,
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/components/<component_key>/update", methods=["GET", "POST"])
def update_hvac_component(customerId, reference_type, reference_id, component_key):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    property_id = str(request.args.get("property_id") or "").strip()
    if not customer:
        return redirect(url_for("customers.customers"))
    if reference_type != "system":
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))
    if property_id and str(hvac_system.get("property_id") or "").strip() != property_id:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    serialized_system = serialize_doc(hvac_system)
    system_type = str(serialized_system.get("system_type", "")).strip()
    allowed_component_keys = _get_allowed_component_keys(system_type)
    if component_key not in allowed_component_keys:
        return redirect(
            url_for(
                "customers.view_hvac_system",
                customerId=customerId,
                reference_type="system",
                reference_id=reference_id,
                property_id=property_id,
            )
        )

    if component_key == "ductwork":
        ductwork = _extract_hvac_ductwork(serialized_system) or {}
        error = ""
        ductwork_error_fields = []
        form_data = {
            "type": ductwork.get("type", ""),
            "insulated": ductwork.get("insulated", ""),
            "supply_branches": ductwork.get("supply_branches", ""),
            "returns": ductwork.get("returns", ""),
            "ductwork_notes": ductwork.get("ductwork_notes", ""),
        }

        if request.method == "POST":
            form_data = {
                "type": request.form.get("type", "").strip(),
                "insulated": request.form.get("insulated", "").strip(),
                "supply_branches": request.form.get("supply_branches", "").strip(),
                "returns": request.form.get("returns", "").strip(),
                "ductwork_notes": request.form.get("ductwork_notes", "").strip(),
            }
            error = _validate_ductwork_data(form_data)
            ductwork_error_fields = _get_missing_ductwork_fields(form_data)
            if error:
                return render_template(
                    "equipment/update_hvac_component.html",
                    customerId=customerId,
                    customer=serialize_doc(customer),
                    hvac_system=serialized_system,
                    reference_type=reference_type,
                    reference_id=reference_id,
                    component_label="Ductwork",
                    component_key=component_key,
                    field_prefix="",
                    is_ductwork=True,
                    form_data=form_data,
                    ductwork_type_options=DUCTWORK_TYPE_OPTIONS,
                    insulated_options=INSULATED_OPTIONS,
                    ductwork_error_fields=ductwork_error_fields,
                    error=error,
                    property_id=property_id,
                )

            if any(form_data.values()):
                db.hvacSystems.update_one(
                    {"$and": [{"_id": hvac_system["_id"]}, build_reference_filter("customer_id", customerId)]},
                    {"$set": {"ductwork": form_data}},
                )
            else:
                db.hvacSystems.update_one(
                    {"$and": [{"_id": hvac_system["_id"]}, build_reference_filter("customer_id", customerId)]},
                    {"$unset": {"ductwork": ""}},
                )

            return redirect(
                url_for(
                    "customers.view_hvac_system",
                    customerId=customerId,
                    reference_type="system",
                    reference_id=reference_id,
                    property_id=property_id,
                )
            )

        return render_template(
            "equipment/update_hvac_component.html",
            customerId=customerId,
            customer=serialize_doc(customer),
            hvac_system=serialized_system,
            reference_type=reference_type,
            reference_id=reference_id,
            component_label="Ductwork",
            component_key=component_key,
            field_prefix="",
            is_ductwork=True,
            form_data=form_data,
            ductwork_type_options=DUCTWORK_TYPE_OPTIONS,
            insulated_options=INSULATED_OPTIONS,
            ductwork_error_fields=ductwork_error_fields,
            error=error,
            property_id=property_id,
        )

    existing_component = _find_existing_hvac_component(db, customerId, serialized_system, component_key)
    serialized_component = serialize_doc(existing_component) if existing_component else {}
    field_prefix = HVAC_COMPONENT_FIELD_BY_COLLECTION.get(component_key, "")
    form_data = {
        "model_number": str(serialized_component.get("model_number", "")).strip(),
        "serial_number": str(serialized_component.get("serial_number", "")).strip(),
        "manufacturer": str(serialized_component.get("manufacturer", "")).strip(),
        "manufacturer_other": str(serialized_component.get("manufacturer_other", "")).strip(),
        "install_year": str(serialized_component.get("install_year", "")).strip(),
        "nickname": str(serialized_component.get("nickname", "")).strip(),
        "thermostat_type": str(serialized_component.get("thermostat_type", "")).strip(),
        "refrigerant_type": str(serialized_component.get("refrigerant_type", "")).strip(),
        "unit_type": str(serialized_component.get("unit_type", "")).strip(),
        "notes": str(serialized_component.get("notes", "")).strip(),
    }

    if request.method == "POST":
        form_data = {
            "model_number": request.form.get("model_number", "").strip(),
            "serial_number": request.form.get("serial_number", "").strip(),
            "manufacturer": request.form.get("manufacturer", "").strip(),
            "manufacturer_other": request.form.get("manufacturer_other", "").strip(),
            "install_year": request.form.get("install_year", "").strip(),
            "nickname": request.form.get("nickname", "").strip(),
            "thermostat_type": request.form.get("thermostat_type", "").strip(),
            "refrigerant_type": request.form.get("refrigerant_type", "").strip(),
            "unit_type": request.form.get("unit_type", "").strip(),
            "notes": request.form.get("notes", "").strip(),
        }
        component_document = {
            "customer_id": reference_value(customerId),
            "system_type": system_type,
            "property_id": serialized_system.get("property_id", ""),
            "hvac_system_id": str(hvac_system.get("_id", "")).strip(),
            **form_data,
        }

        db.hvacSystems.update_one(
            {"$and": [{"_id": hvac_system["_id"]}, build_reference_filter("customer_id", customerId)]},
            {"$set": {f"components.{component_key}": component_document}},
        )

        return redirect(
            url_for(
                "customers.view_hvac_system",
                customerId=customerId,
                reference_type="system",
                reference_id=reference_id,
                property_id=property_id,
            )
        )

    return render_template(
        "equipment/update_hvac_component.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        hvac_system=serialized_system,
        reference_type=reference_type,
        reference_id=reference_id,
        component_label=HVAC_COMPONENT_LABELS.get(component_key, "Component"),
        component_key=component_key,
        field_prefix=field_prefix,
        is_ductwork=False,
        form_data=form_data,
        manufacturer_options=MANUFACTURER_OPTIONS,
        thermostat_type_options=THERMOSTAT_TYPE_OPTIONS,
        thermostat_manufacturer_options=THERMOSTAT_MANUFACTURER_OPTIONS,
        package_unit_type_options=PACKAGE_UNIT_TYPE_OPTIONS,
        refrigerant_type_options=REFRIGERANT_TYPE_OPTIONS,
        property_id=property_id,
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/delete", methods=["POST"])
def delete_hvac_system(customerId, reference_type, reference_id):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    property_id = str(request.args.get("property_id") or "").strip()
    if not customer:
        return redirect(url_for("customers.customers"))
    if reference_type != "system":
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))
    if property_id and str(hvac_system.get("property_id") or "").strip() != property_id:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    db.hvacDiagnostics.delete_many(
        {
            "$and": [
                build_reference_filter("hvac_system_id", reference_id),
                build_reference_filter("customer_id", customerId),
            ]
        }
    )

    db.hvacSystems.delete_one({"$and": [{"_id": hvac_system["_id"]}, build_reference_filter("customer_id", customerId)]})

    if property_id:
        return redirect(url_for("customers.view_property", customerId=customerId, propertyId=property_id))
    return redirect(url_for("customers.view_customer", customerId=customerId))


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/photos/upload", methods=["POST"])
def upload_hvac_photo(customerId, reference_type, reference_id):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    property_id = str(request.args.get("property_id") or "").strip()
    if not customer:
        return redirect(url_for("customers.customers"))
    if reference_type != "system":
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))
    if property_id and str(hvac_system.get("property_id") or "").strip() != property_id:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    photo_file = request.files.get("photo")
    if not photo_file or not str(photo_file.filename or "").strip():
        return redirect(url_for("customers.view_hvac_system", customerId=customerId, reference_type=reference_type, reference_id=reference_id, property_id=property_id, photo_error="missing"))

    filename = str(photo_file.filename or "").strip()
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in ALLOWED_PHOTO_EXTENSIONS:
        return redirect(url_for("customers.view_hvac_system", customerId=customerId, reference_type=reference_type, reference_id=reference_id, property_id=property_id, photo_error="invalid_type"))

    photo_file.seek(0, 2)
    file_size = photo_file.tell()
    photo_file.seek(0)
    if file_size > MAX_PHOTO_FILE_SIZE:
        return redirect(url_for("customers.view_hvac_system", customerId=customerId, reference_type=reference_type, reference_id=reference_id, property_id=property_id, photo_error="too_large"))

    upload_dir = os.path.join(current_app.root_path, "static", HVAC_PHOTO_UPLOAD_SUBDIR)
    os.makedirs(upload_dir, exist_ok=True)

    safe_name = f"{reference_id}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{secure_filename(filename)}"
    save_path = os.path.join(upload_dir, safe_name)
    try:
        photo_file.save(save_path)
    except Exception:
        return redirect(url_for("customers.view_hvac_system", customerId=customerId, reference_type=reference_type, reference_id=reference_id, property_id=property_id, photo_error="upload_failed"))

    photo_entry = {
        "filename": safe_name,
        "uploaded_at": datetime.now().strftime("%m/%d/%Y"),
    }
    db.hvacSystems.update_one(
        {"$and": [{"_id": hvac_system["_id"]}, build_reference_filter("customer_id", customerId)]},
        {"$push": {"photos": photo_entry}},
    )

    return redirect(url_for("customers.view_hvac_system", customerId=customerId, reference_type=reference_type, reference_id=reference_id, property_id=property_id))


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/photos/delete", methods=["POST"])
def delete_hvac_photo(customerId, reference_type, reference_id):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    property_id = str(request.args.get("property_id") or "").strip()
    if not customer:
        return redirect(url_for("customers.customers"))
    if reference_type != "system":
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))
    if property_id and str(hvac_system.get("property_id") or "").strip() != property_id:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    filename_to_delete = str(request.form.get("filename", "")).strip()
    if filename_to_delete:
        file_path = os.path.join(current_app.root_path, "static", HVAC_PHOTO_UPLOAD_SUBDIR, filename_to_delete)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass
        db.hvacSystems.update_one(
            {"$and": [{"_id": hvac_system["_id"]}, build_reference_filter("customer_id", customerId)]},
            {"$pull": {"photos": {"filename": filename_to_delete}}},
        )

    return redirect(url_for("customers.view_hvac_system", customerId=customerId, reference_type=reference_type, reference_id=reference_id, property_id=property_id))


@bp.route("/customers/<customerId>/equipment/add", methods=["GET", "POST"])
def add_equipment(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    property_id = str(request.args.get("property_id") or request.form.get("property_id") or "").strip()
    customer_property = _find_customer_property(customer, property_id) if property_id else None
    if property_id and not customer_property:
        return redirect(url_for("customers.view_customer", customerId=customerId))
    if not property_id:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    error = ""
    ductwork_error_fields = []
    form_data = _build_empty_hvac_form_data()
    form_data["property_id"] = property_id
    if request.method == "POST":
        for field_name in form_data:
            form_data[field_name] = request.form.get(field_name, "").strip()

        system_type = form_data["system_type"]

        if not system_type:
            error = "System type is required."
        elif system_type not in SYSTEM_TYPE_OPTIONS:
            error = "Please select a valid system type."
        else:
            ductwork_data = {
                "type": form_data.get("ductwork_type", ""),
                "insulated": form_data.get("ductwork_insulated", ""),
                "supply_branches": form_data.get("ductwork_supply_branches", ""),
                "returns": form_data.get("ductwork_returns", ""),
                "ductwork_notes": form_data.get("ductwork_notes", ""),
            }
            if system_type in DUCTWORK_SYSTEM_TYPES:
                error = _validate_ductwork_data(ductwork_data)
                ductwork_error_fields = _get_missing_ductwork_fields(ductwork_data)

        if not error:
            base_document = _build_hvac_system_document(customerId, system_type, form_data)

            inserted_hvac_system = db.hvacSystems.insert_one(base_document)
            hvac_system_id = str(inserted_hvac_system.inserted_id)
            component_base_document = {
                **base_document,
                "hvac_system_id": hvac_system_id,
            }
            component_snapshots = {}

            for collection_name, _label in HVAC_COLLECTION_CONFIG.get(system_type, ()):
                component_doc = _build_hvac_component_document(form_data, component_base_document, collection_name)
                component_snapshots[collection_name] = component_doc

            update_payload = {"components": component_snapshots}
            if system_type in DUCTWORK_SYSTEM_TYPES:
                ductwork = _build_hvac_ductwork(form_data)
                if ductwork:
                    update_payload["ductwork"] = ductwork

            db.hvacSystems.update_one(
                {"_id": inserted_hvac_system.inserted_id},
                {"$set": update_payload},
            )

            if property_id:
                return redirect(url_for("customers.view_property", customerId=customerId, propertyId=property_id))
            return redirect(url_for("customers.view_customer", customerId=customerId))

    return render_template(
        "equipment/add_equipment.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        error=error,
        form_action=url_for("customers.add_equipment", customerId=customerId, property_id=property_id),
        page_title="Add HVAC System",
        breadcrumb_current="Add HVAC System",
        submit_label="Save HVAC System",
        form_data=form_data,
        system_type_options=SYSTEM_TYPE_OPTIONS,
        tonnage_options=TONNAGE_OPTIONS,
        ductwork_type_options=DUCTWORK_TYPE_OPTIONS,
        insulated_options=INSULATED_OPTIONS,
        manufacturer_options=MANUFACTURER_OPTIONS,
        mini_split_manufacturer_options=MINI_SPLIT_MANUFACTURER_OPTIONS,
        thermostat_type_options=THERMOSTAT_TYPE_OPTIONS,
        thermostat_manufacturer_options=THERMOSTAT_MANUFACTURER_OPTIONS,
        package_unit_type_options=PACKAGE_UNIT_TYPE_OPTIONS,
        refrigerant_type_options=REFRIGERANT_TYPE_OPTIONS,
        ductwork_error_fields=ductwork_error_fields,
        property_id=property_id,
        property=customer_property,
    )


@bp.route("/customers/<customerId>/equipment/<equipmentId>")
def view_equipment(customerId, equipmentId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    equipment = db.equipment.find_one({"$and": [{"_id": object_id_or_404(equipmentId)}, build_reference_filter("customer_id", customerId)]})
    if not equipment:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    serialized_equipment = serialize_doc(equipment)
    equipment_part_names = [
        (part.get("name") or "").strip()
        for part in serialized_equipment.get("parts", [])
        if (part.get("name") or "").strip()
    ]
    part_lookup = {}

    business_id = _resolve_current_business_id(db)
    if equipment_part_names:
        part_query = {"part_name": {"$in": equipment_part_names}}
        if business_id:
            part_query["business_id"] = business_id
        matching_parts = [
            serialize_doc(part)
            for part in db.parts.find(part_query)
        ]
        part_lookup = {part.get("part_name"): part for part in matching_parts}

    equipment_parts = []
    for part in serialized_equipment.get("parts", []):
        part_name = (part.get("name") or "").strip()
        matched_part = part_lookup.get(part_name)
        equipment_parts.append(
            {
                "name": part_name or "-",
                "price": part.get("price", ""),
                "part_id": matched_part.get("_id") if matched_part else None,
                "code": matched_part.get("part_code", "") if matched_part else part.get("code", ""),
            }
        )

    return render_template(
        "equipment/view_equipment.html",
        customerId=customerId,
        equipmentId=equipmentId,
        customer=serialize_doc(customer),
        equipment=serialized_equipment,
        equipment_parts=equipment_parts,
    )


@bp.route("/customers/<customerId>/equipment/<equipmentId>/update", methods=["GET", "POST"])
def update_equipment(customerId, equipmentId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    equipment = db.equipment.find_one({"$and": [{"_id": object_id_or_404(equipmentId)}, build_reference_filter("customer_id", customerId)]})
    if not equipment:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    error = ""
    business_id = _resolve_current_business_id(db)
    part_query = {"business_id": business_id} if business_id else {"_id": None}
    part_docs = [serialize_doc(part) for part in db.parts.find(part_query).sort("part_name", 1)]
    part_catalog = build_part_catalog(part_docs)
    if request.method == "POST":
        equipment_name = request.form.get("equipment_name", "").strip()
        serial_number = request.form.get("serial_number", "").strip()
        brand = request.form.get("brand", "").strip()
        equipment_location = request.form.get("equipment_location", "").strip()
        equipment_notes = request.form.get("equipment_notes", "").strip()
        selected_part_names = request.form.getlist("part_name[]")
        entered_part_prices = request.form.getlist("part_price[]")
        equipment_parts, _ = build_job_parts_from_form(selected_part_names, entered_part_prices, part_catalog)

        if not equipment_name:
            error = "Equipment type is required."
        elif not brand:
            error = "Brand is required."
        elif not equipment_location:
            error = "Equipment location is required."
        else:
            update_data = {
                "equipment_name": equipment_name,
                "serial_number": serial_number,
                "brand": brand,
                "equipment_location": equipment_location,
                "notes": equipment_notes,
                "parts": equipment_parts,
            }

            db.equipment.update_one({"$and": [{"_id": ObjectId(equipmentId)}, build_reference_filter("customer_id", customerId)]}, {"$set": update_data})
            return redirect(url_for("customers.view_equipment", customerId=customerId, equipmentId=equipmentId))

    return render_template(
        "equipment/update_equipment.html",
        customerId=customerId,
        equipmentId=equipmentId,
        customer=serialize_doc(customer),
        equipment=serialize_doc(equipment),
        parts=part_docs,
        parts_catalog_json=json.dumps(part_catalog),
        error=error,
    )


@bp.route("/customers/<customerId>/equipment/<equipmentId>/delete", methods=["POST"])
def delete_equipment(customerId, equipmentId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    db.equipment.delete_one({"$and": [{"_id": object_id_or_404(equipmentId)}, build_reference_filter("customer_id", customerId)]})
    return redirect(url_for("customers.view_customer", customerId=customerId))
