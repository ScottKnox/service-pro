document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("add-customer-form");
  if (!form) {
    return;
  }

  const firstNameInput = form.querySelector('input[name="first_name"]');
  const lastNameInput = form.querySelector('input[name="last_name"]');
  const emailInput = form.querySelector('input[name="email"]');
  const submitButton = form.querySelector('button[type="submit"]');

  if (!firstNameInput || !lastNameInput || !emailInput || !submitButton) {
    return;
  }

  let firstNameWasPopulated = firstNameInput.value.trim().length > 0;
  let lastNameWasPopulated = lastNameInput.value.trim().length > 0;
  let emailWasPopulated = emailInput.value.trim().length > 0;
  const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  const emailValidationMessage = "Enter a valid email address.";

  const createFieldError = (input) => {
    const error = document.createElement("p");
    error.className = "field-validation-error";
    error.textContent = "";
    input.insertAdjacentElement("afterend", error);
    return error;
  };

  const firstNameError = createFieldError(firstNameInput);
  const lastNameError = createFieldError(lastNameInput);
  const emailError = createFieldError(emailInput);

  const syncFieldError = (input, errorElement, wasPopulatedRef) => {
    const value = input.value.trim();
    if (value.length > 0) {
      wasPopulatedRef.value = true;
      errorElement.textContent = "";
      errorElement.classList.remove("is-visible");
      input.removeAttribute("aria-invalid");
      return;
    }

    if (wasPopulatedRef.value) {
      errorElement.textContent = "This field is required.";
      errorElement.classList.add("is-visible");
      input.setAttribute("aria-invalid", "true");
      return;
    }

    errorElement.textContent = "";
    errorElement.classList.remove("is-visible");
    input.removeAttribute("aria-invalid");
  };

  const syncEmailError = () => {
    const value = emailInput.value.trim();
    if (value.length > 0) {
      emailWasPopulated = true;
      if (!emailPattern.test(value)) {
        emailError.textContent = emailValidationMessage;
        emailError.classList.add("is-visible");
        emailInput.setAttribute("aria-invalid", "true");
        emailInput.setCustomValidity(emailValidationMessage);
        return false;
      }

      emailError.textContent = "";
      emailError.classList.remove("is-visible");
      emailInput.removeAttribute("aria-invalid");
      emailInput.setCustomValidity("");
      return true;
    }

    if (emailWasPopulated) {
      emailError.textContent = "";
      emailError.classList.remove("is-visible");
      emailInput.removeAttribute("aria-invalid");
      emailInput.setCustomValidity("");
    }

    return true;
  };

  const syncSubmitState = () => {
    const hasFirstName = firstNameInput.value.trim().length > 0;
    const hasLastName = lastNameInput.value.trim().length > 0;
    const hasValidEmail = syncEmailError();

    syncFieldError(firstNameInput, firstNameError, { value: firstNameWasPopulated });
    firstNameWasPopulated = firstNameWasPopulated || hasFirstName;

    syncFieldError(lastNameInput, lastNameError, { value: lastNameWasPopulated });
    lastNameWasPopulated = lastNameWasPopulated || hasLastName;

    submitButton.disabled = !(hasFirstName && hasLastName && hasValidEmail);
  };

  firstNameInput.addEventListener("input", syncSubmitState);
  lastNameInput.addEventListener("input", syncSubmitState);
  emailInput.addEventListener("input", syncSubmitState);
  emailInput.addEventListener("blur", syncSubmitState);

  form.addEventListener("submit", (event) => {
    syncSubmitState();

    if (!syncEmailError()) {
      event.preventDefault();
      emailInput.reportValidity();
    }
  });

  syncSubmitState();
});
