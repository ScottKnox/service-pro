"""Security utilities for input validation and NoSQL injection prevention."""

import re
from bson import ObjectId


def validate_object_id(value, param_name="ID"):
    """
    Validate that a value is a valid MongoDB ObjectId string.
    Raises ValueError if invalid.
    
    Args:
        value: String to validate as ObjectId
        param_name: Name of parameter for error message
    
    Returns:
        Validated string
    
    Raises:
        ValueError: If not a valid ObjectId string
    """
    if not value:
        raise ValueError(f"{param_name} is required")
    
    value_str = str(value).strip()
    if not ObjectId.is_valid(value_str):
        raise ValueError(f"Invalid {param_name} format")
    
    return value_str


def validate_email(email):
    """
    Validate email format.
    
    Args:
        email: Email string to validate
    
    Returns:
        Validated email string (lowercased)
    
    Raises:
        ValueError: If email is invalid
    """
    if not email:
        return ""  # Email is optional
    
    email_str = str(email).strip().lower()
    email_pattern = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    
    if not email_pattern.match(email_str):
        raise ValueError("Invalid email format")
    
    return email_str


def validate_phone(phone):
    """
    Validate phone number format (basic).
    
    Args:
        phone: Phone string to validate
    
    Returns:
        Validated phone string
    
    Raises:
        ValueError: If phone is invalid
    """
    if not phone:
        return ""  # Phone is optional
    
    phone_str = str(phone).strip()
    # Allow digits, spaces, hyphens, parentheses, plus sign
    if not re.match(r"^[\d\s\-().+]+$", phone_str):
        raise ValueError("Invalid phone number format")
    
    # Ensure at least 10 digits
    digits_only = re.sub(r"\D", "", phone_str)
    if len(digits_only) < 10:
        raise ValueError("Phone number must contain at least 10 digits")
    
    return phone_str


def validate_string_field(value, field_name, min_length=1, max_length=1000, required=True):
    """
    Validate a string field with length constraints.
    
    Args:
        value: String to validate
        field_name: Name of field for error message
        min_length: Minimum length required
        max_length: Maximum length allowed
        required: Whether field is required
    
    Returns:
        Validated string (stripped)
    
    Raises:
        ValueError: If validation fails
    """
    if not value:
        if required:
            raise ValueError(f"{field_name} is required")
        return ""
    
    value_str = str(value).strip()
    
    if len(value_str) < min_length:
        raise ValueError(f"{field_name} must be at least {min_length} characters")
    
    if len(value_str) > max_length:
        raise ValueError(f"{field_name} cannot exceed {max_length} characters")
    
    return value_str


def validate_numeric_field(value, field_name, allow_negative=False, allow_zero=True):
    """
    Validate a numeric field.
    
    Args:
        value: Value to validate as number
        field_name: Name of field for error message
        allow_negative: Whether negative values allowed
        allow_zero: Whether zero is allowed
    
    Returns:
        Validated float
    
    Raises:
        ValueError: If validation fails
    """
    if value == "" or value is None:
        return 0.0
    
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a valid number")
    
    if not allow_negative and num < 0:
        raise ValueError(f"{field_name} cannot be negative")
    
    if not allow_zero and num == 0:
        raise ValueError(f"{field_name} cannot be zero")
    
    return num


def validate_percentage(value, field_name):
    """
    Validate a percentage field (0-100).
    
    Args:
        value: Value to validate
        field_name: Name of field for error message
    
    Returns:
        Validated float (0-100)
    
    Raises:
        ValueError: If not a valid percentage
    """
    if value == "" or value is None:
        return 0.0
    
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a valid number")
    
    if num < 0 or num > 100:
        raise ValueError(f"{field_name} must be between 0 and 100")
    
    return num


def validate_integer_field(value, field_name, min_value=None, max_value=None):
    """
    Validate an integer field.
    
    Args:
        value: Value to validate as integer
        field_name: Name of field for error message
        min_value: Minimum allowed value
        max_value: Maximum allowed value
    
    Returns:
        Validated integer
    
    Raises:
        ValueError: If validation fails
    """
    if value == "" or value is None:
        return 0
    
    try:
        num = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a valid integer")
    
    if min_value is not None and num < min_value:
        raise ValueError(f"{field_name} cannot be less than {min_value}")
    
    if max_value is not None and num > max_value:
        raise ValueError(f"{field_name} cannot exceed {max_value}")
    
    return num


def validate_choice_field(value, field_name, allowed_choices):
    """
    Validate that value is one of allowed choices.
    
    Args:
        value: Value to validate
        field_name: Name of field for error message
        allowed_choices: List of allowed values
    
    Returns:
        Validated choice
    
    Raises:
        ValueError: If not in allowed choices
    """
    if not value:
        return None
    
    value_str = str(value).strip()
    if value_str not in allowed_choices:
        raise ValueError(f"{field_name} must be one of: {', '.join(allowed_choices)}")
    
    return value_str


def sanitize_string(value):
    """
    Basic string sanitization (remove null bytes and control characters).
    
    Args:
        value: String to sanitize
    
    Returns:
        Sanitized string
    """
    if not value:
        return ""
    
    value_str = str(value)
    # Remove null bytes and control characters
    value_str = value_str.replace("\x00", "")
    value_str = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", value_str)
    
    return value_str


PASSWORD_REQUIREMENTS_MESSAGE = (
    "Password must be at least 8 characters and include at least one uppercase letter, "
    "one number, and one special character from !@#$%^&*."
)
PASSWORD_REQUIREMENTS_PATTERN = re.compile(r"^(?=.*[A-Z])(?=.*\d)(?=.*[!@#$%^&*]).{8,}$")


def password_meets_requirements(password):
    """Return True if the password satisfies the application's strength rules."""
    return bool(PASSWORD_REQUIREMENTS_PATTERN.match(password or ""))
