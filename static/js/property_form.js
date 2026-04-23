document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector("[data-property-form]");
  if (!form) {
    return;
  }

  const propertyTypeSelect = form.querySelector("[data-property-type-select]");
  const propertyTypeOtherWrap = form.querySelector("[data-property-type-other-wrap]");
  const propertyTypeOtherInput = form.querySelector("[data-property-type-other-input]");
  const submitButton = form.querySelector('button[type="submit"]');

  if (!propertyTypeSelect || !submitButton) {
    return;
  }

  const requiredFieldSelectors = [
    'input[name="property_name"]',
    'select[name="property_type"]',
    'input[name="address_line_1"]',
    'input[name="city"]',
    'input[name="state"]',
    'input[name="zip_code"]',
  ];

  const requiredFields = requiredFieldSelectors
    .map((selector) => form.querySelector(selector))
    .filter(Boolean);

  const syncOtherTypeVisibility = () => {
    const isOther = propertyTypeSelect.value === "other";
    if (propertyTypeOtherWrap) {
      propertyTypeOtherWrap.hidden = !isOther;
    }
    if (propertyTypeOtherInput) {
      propertyTypeOtherInput.required = isOther;
      if (!isOther) {
        propertyTypeOtherInput.value = "";
      }
    }
  };

  const hasValue = (field) => {
    return !!field && field.value.trim().length > 0;
  };

  const syncSubmitState = () => {
    let isValid = requiredFields.every(hasValue);

    if (propertyTypeSelect.value === "other") {
      isValid = isValid && hasValue(propertyTypeOtherInput);
    }

    submitButton.disabled = !isValid;
  };

  requiredFields.forEach((field) => {
    field.addEventListener("input", syncSubmitState);
    field.addEventListener("change", syncSubmitState);
  });

  if (propertyTypeOtherInput) {
    propertyTypeOtherInput.addEventListener("input", syncSubmitState);
    propertyTypeOtherInput.addEventListener("change", syncSubmitState);
  }

  propertyTypeSelect.addEventListener("change", () => {
    syncOtherTypeVisibility();
    syncSubmitState();
  });

  syncOtherTypeVisibility();
  syncSubmitState();
});
