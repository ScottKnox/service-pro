(function () {
  const form = document.getElementById('add-customer-form');
  const addPartButton = document.getElementById('add-job-part-button');
  const partsList = document.getElementById('job-parts-list');
  const partOptionsTemplate = document.getElementById('part-options-template');
  const submitButton = form ? form.querySelector('button[type="submit"]') : null;

  if (!form || !partsList || !partOptionsTemplate) {
    return;
  }

  const partsCatalog = form.dataset.partsCatalog ? JSON.parse(form.dataset.partsCatalog) : {};
  const requiredFieldStates = [
    {
      field: document.getElementById('equipment-name'),
      error: document.getElementById('error-equipment-type'),
      wasPopulated: false,
    },
    {
      field: document.getElementById('equipment-brand'),
      error: document.getElementById('error-brand'),
      wasPopulated: false,
    },
    {
      field: document.getElementById('equipment-location'),
      error: document.getElementById('error-location'),
      wasPopulated: false,
    },
  ].filter(function (state) {
    return !!state.field;
  });

  requiredFieldStates.forEach(function (state) {
    state.wasPopulated = (state.field.value || '').trim().length > 0;
    if (state.error) {
      state.error.textContent = '';
      state.error.classList.remove('is-visible');
    }
  });

  function syncRequiredField(state) {
    const fieldValue = (state.field.value || '').trim();
    if (fieldValue.length > 0) {
      state.wasPopulated = true;
      if (state.error) {
        state.error.textContent = '';
        state.error.classList.remove('is-visible');
      }
      state.field.removeAttribute('aria-invalid');
      return true;
    }

    if (state.wasPopulated) {
      if (state.error) {
        state.error.textContent = 'This field is required.';
        state.error.classList.add('is-visible');
      }
      state.field.setAttribute('aria-invalid', 'true');
      return false;
    }

    if (state.error) {
      state.error.textContent = '';
      state.error.classList.remove('is-visible');
    }
    state.field.removeAttribute('aria-invalid');
    return false;
  }

  function syncSubmitState() {
    let isValid = true;

    requiredFieldStates.forEach(function (state) {
      const hasValue = (state.field.value || '').trim().length > 0;
      if (!hasValue) {
        isValid = false;
      }

      syncRequiredField(state);
    });

    if (submitButton) {
      submitButton.disabled = !isValid;
    }

    return isValid;
  }

  function getPartSelect(rowElement) {
    return rowElement ? rowElement.querySelector('select[name="part_name[]"]') : null;
  }

  function isPartRowPopulated(rowElement) {
    const select = getPartSelect(rowElement);
    return !!(select && select.value.trim() !== '');
  }

  function updateRemoveButtonVisibility(rowElement) {
    const removeWrap = rowElement ? rowElement.querySelector('.job-row-remove-wrap') : null;
    if (!removeWrap) {
      return;
    }

    const rowCollection = partsList ? partsList.querySelectorAll('.job-part-row') : [];
    const hasMultipleRows = rowCollection.length > 1;
    const isPopulated = isPartRowPopulated(rowElement);

    removeWrap.style.display = (isPopulated || hasMultipleRows) ? '' : 'none';
  }

  function refreshRemoveButtons() {
    if (!partsList) {
      return;
    }

    const rows = partsList.querySelectorAll('.job-part-row');
    rows.forEach(function (row) {
      updateRemoveButtonVisibility(row);
    });
  }

  function ensureAtLeastOnePartRow() {
    if (!partsList || !partOptionsTemplate) {
      return;
    }

    const rows = partsList.querySelectorAll('.job-part-row');
    if (rows.length === 0) {
      partsList.appendChild(createPartRow());
    }
  }

  function attachRowRemoveButton(rowElement) {
    if (!rowElement || rowElement.querySelector('.job-row-remove-button')) {
      return;
    }

    const removeWrap = document.createElement('div');
    removeWrap.className = 'job-row-remove-wrap';

    const removeButton = document.createElement('button');
    removeButton.type = 'button';
    removeButton.className = 'job-row-remove-button';
    removeButton.textContent = 'Remove Part';

    removeButton.addEventListener('click', function () {
      rowElement.remove();
      ensureAtLeastOnePartRow();
      refreshRemoveButtons();
    });

    removeWrap.appendChild(removeButton);
    rowElement.appendChild(removeWrap);
    updateRemoveButtonVisibility(rowElement);
  }

  function getNextPartRowIndex() {
    const rows = partsList ? partsList.querySelectorAll('.job-part-row') : [];
    let maxIndex = 0;

    rows.forEach(function (row) {
      const parsed = Number.parseInt(row.dataset.partIndex, 10);
      if (!Number.isNaN(parsed) && parsed > maxIndex) {
        maxIndex = parsed;
      }
    });

    return maxIndex + 1;
  }

  function createPartRow() {
    const nextIndex = getNextPartRowIndex();
    const row = document.createElement('div');
    row.className = 'add-customer-form-row job-part-row';
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

    const newSelect = row.querySelector('select[name="part_name[]"]');
    if (newSelect) {
      attachPartPriceUpdateListener(newSelect);
    }

    attachRowRemoveButton(row);
    updateRemoveButtonVisibility(row);
    return row;
  }

  function attachPartPriceUpdateListener(selectElement) {
    selectElement.addEventListener('change', function () {
      const selectedPart = this.value;
      const partIndex = this.id.split('-')[2];
      const priceInput = document.getElementById('part-price-' + partIndex);
      const partDetails = selectedPart ? partsCatalog[selectedPart] : null;

      if (!priceInput) {
        return;
      }

      if (partDetails && partDetails.price) {
        priceInput.value = partDetails.price;
      } else if (!selectedPart) {
        priceInput.value = '$0.00';
      }

      const partRow = this.closest('.job-part-row');
      if (partRow) {
        updateRemoveButtonVisibility(partRow);
      }

      refreshRemoveButtons();
      syncSubmitState();
    });
  }

  const initialPartSelects = document.querySelectorAll('select[id^="part-name-"]');
  initialPartSelects.forEach(function (select) {
    attachPartPriceUpdateListener(select);
  });

  const initialPartRows = partsList ? partsList.querySelectorAll('.job-part-row') : [];
  initialPartRows.forEach(function (row) {
    attachRowRemoveButton(row);
  });

  ensureAtLeastOnePartRow();
  refreshRemoveButtons();

  requiredFieldStates.forEach(function (state) {
    state.field.addEventListener('input', syncSubmitState);
    state.field.addEventListener('change', syncSubmitState);
    state.field.addEventListener('blur', syncSubmitState);
  });

  form.addEventListener('submit', function (event) {
    syncSubmitState();

    if (submitButton && submitButton.disabled) {
      event.preventDefault();
    }
  });

  if (addPartButton) {
    addPartButton.addEventListener('click', function () {
      partsList.appendChild(createPartRow());
      refreshRemoveButtons();
      syncSubmitState();
    });
  }

  form.addEventListener('input', syncSubmitState);
  form.addEventListener('change', syncSubmitState);

  syncSubmitState();
})();
