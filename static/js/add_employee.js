document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("add-customer-form");
  const firstNameInput = document.getElementById("employee-first-name");
  const lastNameInput = document.getElementById("employee-last-name");
  const usernameInput = document.getElementById("employee-username");
  const passwordInput = document.getElementById("employee-password");
  const phoneInput = document.getElementById("employee-phone");
  const emailInput = document.getElementById("employee-email");
  const positionInput = document.getElementById("employee-position");
  const passwordError = document.getElementById("employee-password-error");
  const submitButton = form ? form.querySelector('button[type="submit"]') : null;

  if (
    !form
    || !firstNameInput
    || !lastNameInput
    || !usernameInput
    || !passwordInput
    || !phoneInput
    || !emailInput
    || !positionInput
    || !passwordError
    || !submitButton
  ) {
    return;
  }

  const passwordRequirements = /^(?=.*[A-Z])(?=.*\d)(?=.*[!@#$%^&*]).{8,}$/;
  const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  const passwordRequirementsMessage = "Password must be at least 8 characters and include at least one uppercase letter, one number, and one special character from !@#$%^&*.";
  const emailValidationMessage = "Enter a valid email address.";
  const requiredFieldMessage = "This field is required.";

  const createFieldError = (input) => {
    const error = document.createElement("p");
    error.className = "field-validation-error";
    error.setAttribute("aria-live", "polite");
    input.insertAdjacentElement("afterend", error);
    return error;
  };

  const requiredFields = [
    {
      input: firstNameInput,
      errorElement: createFieldError(firstNameInput),
      wasPopulated: firstNameInput.value.trim().length > 0,
      getValue: () => firstNameInput.value.trim(),
    },
    {
      input: lastNameInput,
      errorElement: createFieldError(lastNameInput),
      wasPopulated: lastNameInput.value.trim().length > 0,
      getValue: () => lastNameInput.value.trim(),
    },
    {
      input: usernameInput,
      errorElement: createFieldError(usernameInput),
      wasPopulated: usernameInput.value.trim().length > 0,
      getValue: () => usernameInput.value.trim(),
    },
    {
      input: passwordInput,
      errorElement: passwordError,
      wasPopulated: passwordInput.value.length > 0,
      getValue: () => passwordInput.value,
      customValidate: (value) => {
        if (!passwordRequirements.test(value)) {
          return passwordRequirementsMessage;
        }

        return "";
      },
    },
    {
      input: phoneInput,
      errorElement: createFieldError(phoneInput),
      wasPopulated: phoneInput.value.trim().length > 0,
      getValue: () => phoneInput.value.trim(),
    },
    {
      input: emailInput,
      errorElement: createFieldError(emailInput),
      wasPopulated: emailInput.value.trim().length > 0,
      getValue: () => emailInput.value.trim(),
      customValidate: (value) => {
        if (!emailPattern.test(value)) {
          return emailValidationMessage;
        }

        return "";
      },
    },
    {
      input: positionInput,
      errorElement: createFieldError(positionInput),
      wasPopulated: positionInput.value.trim().length > 0,
      getValue: () => positionInput.value.trim(),
    },
  ];

  const setFieldError = (field, message) => {
    field.errorElement.textContent = message;
    field.errorElement.classList.toggle("is-visible", Boolean(message));
    field.input.setAttribute("aria-invalid", message ? "true" : "false");
    field.input.setCustomValidity(message);
  };

  const validateField = (field) => {
    const value = field.getValue();
    const hasValue = value.length > 0;

    if (hasValue) {
      field.wasPopulated = true;
    }

    if (!hasValue) {
      if (field.wasPopulated) {
        setFieldError(field, requiredFieldMessage);
        return false;
      }

      setFieldError(field, "");
      return true;
    }

    const customValidationMessage = field.customValidate ? field.customValidate(value) : "";
    if (customValidationMessage) {
      setFieldError(field, customValidationMessage);
      return false;
    }

    setFieldError(field, "");
    return true;
  };

  const syncSubmitState = () => {
    const hasAllRequiredValues = requiredFields.every((field) => field.getValue().length > 0);
    const allFieldsValid = requiredFields.every((field) => validateField(field));
    submitButton.disabled = !(hasAllRequiredValues && allFieldsValid);
  };

  requiredFields.forEach((field) => {
    const eventName = field.input.tagName === "SELECT" ? "change" : "input";

    field.input.addEventListener(eventName, () => {
      validateField(field);
      syncSubmitState();
    });

    field.input.addEventListener("blur", () => {
      validateField(field);
      syncSubmitState();
    });
  });

  form.addEventListener("submit", (event) => {
    usernameInput.value = usernameInput.value.trimEnd();

    const allFieldsValid = requiredFields.every((field) => validateField(field));
    syncSubmitState();

    if (!allFieldsValid) {
      event.preventDefault();
      const firstInvalidField = requiredFields.find((field) => !validateField(field));
      if (firstInvalidField) {
        firstInvalidField.input.reportValidity();
      }
    }
  });

  syncSubmitState();
});