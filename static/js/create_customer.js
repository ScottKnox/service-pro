document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("add-customer-form");
  if (!form) {
    return;
  }

  const firstNameInput = form.querySelector('input[name="first_name"]');
  const lastNameInput = form.querySelector('input[name="last_name"]');
  const submitButton = form.querySelector('button[type="submit"]');

  if (!firstNameInput || !lastNameInput || !submitButton) {
    return;
  }

  let firstNameWasPopulated = firstNameInput.value.trim().length > 0;
  let lastNameWasPopulated = lastNameInput.value.trim().length > 0;

  const createFieldError = (input) => {
    const error = document.createElement("p");
    error.className = "field-validation-error";
    error.textContent = "";
    input.insertAdjacentElement("afterend", error);
    return error;
  };

  const firstNameError = createFieldError(firstNameInput);
  const lastNameError = createFieldError(lastNameInput);

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

  const syncSubmitState = () => {
    const hasFirstName = firstNameInput.value.trim().length > 0;
    const hasLastName = lastNameInput.value.trim().length > 0;

    syncFieldError(firstNameInput, firstNameError, { value: firstNameWasPopulated });
    firstNameWasPopulated = firstNameWasPopulated || hasFirstName;

    syncFieldError(lastNameInput, lastNameError, { value: lastNameWasPopulated });
    lastNameWasPopulated = lastNameWasPopulated || hasLastName;

    submitButton.disabled = !(hasFirstName && hasLastName);
  };

  firstNameInput.addEventListener("input", syncSubmitState);
  lastNameInput.addEventListener("input", syncSubmitState);
  syncSubmitState();
});
