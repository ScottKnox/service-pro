(function () {
  const categoryFields = document.querySelectorAll('[data-category-field]');

  function ensureOption(select, value) {
    const normalized = String(value || '').trim();
    if (!normalized) {
      return;
    }

    const exists = Array.from(select.options).some(function (option) {
      return option.value === normalized;
    });

    if (!exists) {
      const option = document.createElement('option');
      option.value = normalized;
      option.textContent = normalized;
      select.appendChild(option);
    }
  }

  categoryFields.forEach(function (fieldRoot) {
    const select = fieldRoot.querySelector('[data-category-select]');
    const input = fieldRoot.querySelector('[data-category-input]');
    const toggleButton = fieldRoot.querySelector('[data-category-toggle]');

    if (!select || !input || !toggleButton) {
      return;
    }

    const fieldName = select.getAttribute('name') || input.getAttribute('data-field-name') || 'category';
    const isRequired = select.required;

    function useSelectMode() {
      const typedValue = String(input.value || '').trim();
      if (typedValue) {
        ensureOption(select, typedValue);
        select.value = typedValue;
      }

      select.disabled = false;
      select.setAttribute('name', fieldName);
      select.required = isRequired;

      input.disabled = true;
      input.removeAttribute('name');
      input.required = false;
      input.hidden = true;

      toggleButton.textContent = '+';
      toggleButton.setAttribute('aria-label', 'Add new category');
      toggleButton.title = 'Add new category';
    }

    function useInputMode() {
      const selectedValue = String(select.value || '').trim();
      if (!input.value.trim()) {
        input.value = selectedValue;
      }

      select.disabled = true;
      select.removeAttribute('name');
      select.required = false;

      input.disabled = false;
      input.setAttribute('name', fieldName);
      input.required = isRequired;
      input.hidden = false;
      input.focus();

      toggleButton.textContent = '↺';
      toggleButton.setAttribute('aria-label', 'Use existing category');
      toggleButton.title = 'Use existing category';
    }

    toggleButton.addEventListener('click', function () {
      if (input.hidden) {
        useInputMode();
      } else {
        useSelectMode();
      }
    });

    input.addEventListener('blur', function () {
      const typedValue = String(input.value || '').trim();
      if (typedValue) {
        ensureOption(select, typedValue);
      }
    });

    useSelectMode();
  });
})();
