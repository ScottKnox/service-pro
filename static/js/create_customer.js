document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("add-customer-form");
  if (!form) {
    return;
  }

  const firstNameInput = form.querySelector('input[name="first_name"]');
  const lastNameInput = form.querySelector('input[name="last_name"]');
  const emailInput = form.querySelector('input[name="email"]');
  const customerTypeSelect = form.querySelector('[data-customer-type-select]');
  const customerTypeNote = form.querySelector('[data-customer-type-note]');
  const submitButton = form.querySelector('button[type="submit"]');

  if (!firstNameInput || !lastNameInput || !emailInput || !submitButton) {
    return;
  }

  let firstNameWasPopulated = firstNameInput.value.trim().length > 0;
  let lastNameWasPopulated = lastNameInput.value.trim().length > 0;
  let emailWasPopulated = emailInput.value.trim().length > 0;
  const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  const emailValidationMessage = "Enter a valid email address.";
  const customerTypeNotes = {
    Residential: "Single-family homes, condos & townhouses, apartments, mobile homes, new constructions.",
    Commercial: "Office buildings, Retail stores, Restaurants, Hotels, Medical offices, Gyms, Car dealerships, Banks.",
    Industrial: "Warehouses, Manufacturing, Refrigeration facilities, Data centers, Auto shops.",
    Institutional: "Schools, Hospitals, Government, Churches, Community centers.",
    Specialty: "Multi-tenant commercial buildings (landlord contracts), HOA-managed communities, Property management portfolios (one contract covering many units), New construction (builder contracts - ongoing relationship across multiple builds).",
  };

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

  const syncCustomerTypeNote = () => {
    if (!customerTypeSelect || !customerTypeNote) {
      return;
    }

    const selectedType = customerTypeSelect.value || "Residential";
    customerTypeNote.textContent = customerTypeNotes[selectedType] || "";
  };

  firstNameInput.addEventListener("input", syncSubmitState);
  lastNameInput.addEventListener("input", syncSubmitState);
  emailInput.addEventListener("input", syncSubmitState);
  emailInput.addEventListener("blur", syncSubmitState);
  if (customerTypeSelect) {
    customerTypeSelect.addEventListener("change", syncCustomerTypeNote);
  }

  form.addEventListener("submit", (event) => {
    syncSubmitState();

    if (!syncEmailError()) {
      event.preventDefault();
      emailInput.reportValidity();
    }
  });

  syncSubmitState();
  syncCustomerTypeNote();
});
