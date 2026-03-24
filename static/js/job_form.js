(function () {
  const form = document.getElementById('create-job-form');
  const submitButton = document.getElementById('submit-button');

  // Get service catalog from form data attribute
  const servicesCatalog = form && form.dataset.servicesCatalog ? JSON.parse(form.dataset.servicesCatalog) : {};
  const partsCatalog = form && form.dataset.partsCatalog ? JSON.parse(form.dataset.partsCatalog) : {};

  // Track user interaction for specific fields
  let servicesTouched = false;
  let dateTouched = false;
  let employeeTouched = false;

  // Validation function
  function validateForm() {
    let isValid = true;

    // Get all inputs and selects that are required
    const requiredFields = [
      'job-address-line-1',
      'job-city',
      'job-state',
      'job-date',
      'job-assigned-employee'
    ];

    requiredFields.forEach(function (fieldId) {
      const field = document.getElementById(fieldId);
      const errorElement = document.getElementById('error-' + fieldId.replace('job-', ''));

      if (!field) return;

      const isFieldValid = field.value.trim() !== '';
      if (!isFieldValid) {
        isValid = false;
        if (errorElement) errorElement.style.display = 'block';
      } else {
        if (errorElement) errorElement.style.display = 'none';
      }
    });

    // Check if at least one service is selected (only show error if touched)
    const serviceSelects = document.querySelectorAll('select[id^="service-type-"]');
    let hasSelectedService = false;
    serviceSelects.forEach(function (select) {
      if (select.value.trim() !== '') {
        hasSelectedService = true;
      }
    });

    const servicesError = document.getElementById('error-services');
    if (!hasSelectedService) {
      isValid = false;
      if (servicesError) servicesError.style.display = servicesTouched ? 'block' : 'none';
    } else {
      if (servicesError) servicesError.style.display = 'none';
    }

    // Handle date field error visibility based on touch status
    const dateField = document.getElementById('job-date');
    const employeeField = document.getElementById('job-assigned-employee');
    const jobDetailsError = document.getElementById('error-job-details');
    
    let jobDetailsErrors = [];
    
    if (dateField && dateField.value.trim() === '' && dateTouched) {
      isValid = false;
      jobDetailsErrors.push('Scheduled Date is required');
    }
    
    if (employeeField && employeeField.value.trim() === '' && employeeTouched) {
      isValid = false;
      jobDetailsErrors.push('Assigned Employee is required');
    }
    
    if (jobDetailsError) {
      if (jobDetailsErrors.length > 0) {
        jobDetailsError.textContent = jobDetailsErrors.join(' • ');
        jobDetailsError.style.display = 'block';
      } else {
        jobDetailsError.style.display = 'none';
      }
    }

    // Update submit button state
    submitButton.disabled = !isValid;

    return isValid;
  }

  // Function to update price and duration when service is selected
  function attachPriceUpdateListener(selectElement) {
    selectElement.addEventListener('change', function () {
      servicesTouched = true; // Mark services as touched
      const selectedService = this.value;
      const serviceIndex = this.id.split('-')[2]; // Extract index from id like 'service-type-1'
      const priceInput = document.getElementById('service-price-' + serviceIndex);
      const durationInput = document.getElementById('service-duration-' + serviceIndex);
      const serviceDetails = selectedService ? servicesCatalog[selectedService] : null;
      
      if (priceInput) {
        if (serviceDetails && serviceDetails.price) {
          priceInput.value = serviceDetails.price;
        } else if (!selectedService) {
          priceInput.value = '$0.00';
        }
      }

      if (durationInput) {
        if (serviceDetails && serviceDetails.duration) {
          durationInput.value = serviceDetails.duration;
        } else if (!selectedService) {
          durationInput.value = '';
        }
      }

      validateForm();
    });
  }

  // Attach listeners to initial service selects
  const initialSelects = document.querySelectorAll('select[id^="service-type-"]');
  initialSelects.forEach(function (select) {
    attachPriceUpdateListener(select);
  });

  const addServiceButton = document.getElementById("add-job-service-button");
  const servicesList = document.getElementById("job-services-list");
  const serviceOptionsTemplate = document.getElementById("service-options-template");
  const addPartButton = document.getElementById("add-job-part-button");
  const partsList = document.getElementById("job-parts-list");
  const partOptionsTemplate = document.getElementById("part-options-template");

  if (!addServiceButton || !servicesList || !serviceOptionsTemplate) {
    return;
  }

  addServiceButton.addEventListener("click", function () {
    const serviceRows = servicesList.querySelectorAll(".job-service-row");
    const nextIndex = serviceRows.length + 1;

    const row = document.createElement("div");
    row.className = "add-customer-form-row job-service-row";
    row.dataset.serviceIndex = String(nextIndex);

    const optionHtml = serviceOptionsTemplate.innerHTML;

    row.innerHTML =
      '<div class="add-customer-form-field">' +
        '<label for="service-type-' + nextIndex + '">Service Type</label>' +
        '<select id="service-type-' + nextIndex + '" name="service_type[]">' +
          '<option value="">-- Select a service --</option>' +
          optionHtml +
        '</select>' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="service-price-' + nextIndex + '">Price</label>' +
        '<input id="service-price-' + nextIndex + '" name="service_price[]" type="text" placeholder="$0.00" />' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="service-duration-' + nextIndex + '">Duration</label>' +
        '<input id="service-duration-' + nextIndex + '" name="service_duration[]" type="text" placeholder="e.g. 2 hours" />' +
      '</div>';

    servicesList.appendChild(row);

    // Attach listener to the newly created select
    const newSelect = row.querySelector('select');
    if (newSelect) {
      attachPriceUpdateListener(newSelect);
    }

    validateForm();
  });

  function attachPartPriceUpdateListener(selectElement) {
    selectElement.addEventListener('change', function () {
      const selectedPart = this.value;
      const partIndex = this.id.split('-')[2];
      const priceInput = document.getElementById('part-price-' + partIndex);
      const partDetails = selectedPart ? partsCatalog[selectedPart] : null;

      if (priceInput) {
        if (partDetails && partDetails.price) {
          priceInput.value = partDetails.price;
        } else if (!selectedPart) {
          priceInput.value = '$0.00';
        }
      }
    });
  }

  const initialPartSelects = document.querySelectorAll('select[id^="part-name-"]');
  initialPartSelects.forEach(function (select) {
    attachPartPriceUpdateListener(select);
  });

  if (addPartButton && partsList && partOptionsTemplate) {
    addPartButton.addEventListener("click", function () {
      const partRows = partsList.querySelectorAll(".job-part-row");
      const nextIndex = partRows.length + 1;

      const row = document.createElement("div");
      row.className = "add-customer-form-row job-part-row";
      row.dataset.partIndex = String(nextIndex);

      const optionHtml = partOptionsTemplate.innerHTML;

      row.innerHTML =
        '<div class="add-customer-form-field">' +
          '<label for="part-name-' + nextIndex + '">Part</label>' +
          '<select id="part-name-' + nextIndex + '" name="part_name[]">' +
            '<option value="">-- Select a part --</option>' +
            optionHtml +
          '</select>' +
        '</div>' +
        '<div class="add-customer-form-field">' +
          '<label for="part-price-' + nextIndex + '">Price</label>' +
          '<input id="part-price-' + nextIndex + '" name="part_price[]" type="text" placeholder="$0.00" />' +
        '</div>';

      partsList.appendChild(row);

      const newSelect = row.querySelector('select');
      if (newSelect) {
        attachPartPriceUpdateListener(newSelect);
      }
    });
  }

  // Add event listeners to validate on input change
  const fieldsToValidate = [
    'job-address-line-1',
    'job-city',
    'job-state',
    'job-date',
    'job-assigned-employee'
  ];

  fieldsToValidate.forEach(function (fieldId) {
    const field = document.getElementById(fieldId);
    if (field) {
      field.addEventListener('change', function () {
        if (fieldId === 'job-date') {
          dateTouched = true;
        } else if (fieldId === 'job-assigned-employee') {
          employeeTouched = true;
        }
        validateForm();
      });
      field.addEventListener('input', function () {
        if (fieldId === 'job-date') {
          dateTouched = true;
        } else if (fieldId === 'job-assigned-employee') {
          employeeTouched = true;
        }
        validateForm();
      });
    }
  });

  // Validate on form submission
  form.addEventListener('submit', function (event) {
    if (!validateForm()) {
      event.preventDefault();
    }
  });

  // Initial validation
  validateForm();
})();
