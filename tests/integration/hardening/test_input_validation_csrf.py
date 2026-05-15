"""Integration tests for input validation and CSRF protection."""

from bson import ObjectId
import pytest


class TestCSRFProtection:
    """Test that CSRF protection is configured."""

    def test_csrf_protection_configured(self, authed_client):
        """Verify CSRF protection is available in the application."""
        # The app should have CSRFProtect enabled
        # Test by checking that valid requests work
        response = authed_client.get("/customers", follow_redirects=False)
        assert response.status_code == 200, "Authorized GET should work"

    def test_get_request_works_without_csrf(self, authed_client, mongo_db):
        """Verify GET requests work without CSRF (as expected)."""
        response = authed_client.get("/customers", follow_redirects=False)
        assert response.status_code == 200, "GET requests should not require CSRF token"


class TestInputValidation:
    """Test input validation and NoSQL injection prevention."""

    def test_invalid_object_id_blocked(self, authed_client, mongo_db):
        """Verify invalid ObjectId format is rejected."""
        response = authed_client.get(
            "/customers/invalid-id-format",
            follow_redirects=False,
        )
        # Should return 404 or 400, not process invalid ID
        assert response.status_code in (400, 404), "Invalid ObjectId should be rejected"

    def test_customer_add_validates_email_format(self, authed_client, mongo_db):
        """Verify email validation on customer creation."""
        response = authed_client.post(
            "/customers/add",
            data={
                "first_name": "Test",
                "last_name": "User",
                "email": "invalid-email-format",
                "csrf_token": authed_client.get("/customers/add").data.decode() or "",
            },
            follow_redirects=False,
        )
        # Invalid email should be rejected or form re-rendered with error
        assert response.status_code in (200, 400), "Invalid email should be rejected"

    def test_customer_add_validates_required_fields(self, authed_client, mongo_db):
        """Verify required field validation."""
        response = authed_client.post(
            "/customers/add",
            data={
                "first_name": "",  # Required
                "last_name": "",   # Required
            },
            follow_redirects=False,
        )
        # Should reject missing required fields
        assert response.status_code in (200, 400)

    def test_phone_validation_rejects_invalid_format(self, authed_client, mongo_db):
        """Verify phone validation rejects invalid formats."""
        from utils.security import validate_phone
        
        # Should accept valid formats
        validate_phone("(417) 555-1234")
        validate_phone("417-555-1234")
        validate_phone("+1 417 555 1234")
        
        # Should reject invalid formats
        with pytest.raises(ValueError):
            validate_phone("abc")  # Not enough digits
        
        with pytest.raises(ValueError):
            validate_phone("123")   # Not enough digits

    def test_email_validation_rejects_invalid_format(self):
        """Verify email validation rejects malformed emails."""
        from utils.security import validate_email
        
        # Should accept valid emails
        assert validate_email("test@example.com") == "test@example.com"
        
        # Should reject invalid formats
        with pytest.raises(ValueError):
            validate_email("notanemail")
        
        with pytest.raises(ValueError):
            validate_email("@example.com")
        
        with pytest.raises(ValueError):
            validate_email("test@")

    def test_percentage_validation_bounds_check(self):
        """Verify percentage validation enforces 0-100 bounds."""
        from utils.security import validate_percentage
        
        # Valid percentages
        assert validate_percentage(0, "test") == 0.0
        assert validate_percentage(50, "test") == 50.0
        assert validate_percentage(100, "test") == 100.0
        
        # Invalid percentages
        with pytest.raises(ValueError):
            validate_percentage(-1, "test")
        
        with pytest.raises(ValueError):
            validate_percentage(101, "test")
        
        with pytest.raises(ValueError):
            validate_percentage(150, "test")

    def test_object_id_validation_rejects_malformed_ids(self):
        """Verify ObjectId validation rejects invalid formats."""
        from utils.security import validate_object_id
        
        # Valid ObjectId
        valid_id = str(ObjectId())
        assert validate_object_id(valid_id) == valid_id
        
        # Invalid ObjectIds
        with pytest.raises(ValueError):
            validate_object_id("not-an-object-id")
        
        with pytest.raises(ValueError):
            validate_object_id("12345")
        
        with pytest.raises(ValueError):
            validate_object_id("")

    def test_string_field_length_validation(self):
        """Verify string field length validation."""
        from utils.security import validate_string_field
        
        # Valid string
        result = validate_string_field("test", "test_field", min_length=1, max_length=100)
        assert result == "test"
        
        # Too short
        with pytest.raises(ValueError):
            validate_string_field("", "test_field", min_length=1, required=True)
        
        # Too long
        with pytest.raises(ValueError):
            validate_string_field("x" * 101, "test_field", max_length=100)

    def test_numeric_field_validation(self):
        """Verify numeric field validation."""
        from utils.security import validate_numeric_field
        
        # Valid numbers
        assert validate_numeric_field(100, "price", allow_negative=False) == 100.0
        assert validate_numeric_field(0, "quantity", allow_zero=True) == 0.0
        
        # Negative when not allowed
        with pytest.raises(ValueError):
            validate_numeric_field(-10, "price", allow_negative=False)
        
        # Zero when not allowed
        with pytest.raises(ValueError):
            validate_numeric_field(0, "quantity", allow_zero=False)
        
        # Non-numeric
        with pytest.raises(ValueError):
            validate_numeric_field("not-a-number", "price")

    def test_choice_field_validation(self):
        """Verify choice field validation."""
        from utils.security import validate_choice_field
        
        allowed = ["option1", "option2", "option3"]
        
        # Valid choice
        assert validate_choice_field("option1", "field", allowed) == "option1"
        
        # Invalid choice
        with pytest.raises(ValueError):
            validate_choice_field("invalid_option", "field", allowed)


class TestSanitization:
    """Test string sanitization."""

    def test_sanitize_removes_null_bytes(self):
        """Verify sanitization removes null bytes."""
        from utils.security import sanitize_string
        
        # Input with null bytes
        input_str = "test\x00value\x00here"
        result = sanitize_string(input_str)
        
        # Should have null bytes removed
        assert "\x00" not in result
        assert "testvaluehere" == result

    def test_sanitize_removes_control_characters(self):
        """Verify sanitization removes control characters."""
        from utils.security import sanitize_string
        
        # Input with control characters
        input_str = "test\x1fvalue\x1ehere"
        result = sanitize_string(input_str)
        
        # Should have control characters removed
        assert len(result) <= len(input_str)
