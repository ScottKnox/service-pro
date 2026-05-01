(function () {
  const customersGrid = document.getElementById('customers-list-grid');
  if (!customersGrid) {
    return;
  }

  const customerCards = Array.from(customersGrid.querySelectorAll('.customer-card'));
  if (customerCards.length === 0) {
    return;
  }

  const statusOptions = document.getElementById('customers-filter-status-options');
  const typeOptions = document.getElementById('customers-filter-type-options');
  const cityOptions = document.getElementById('customers-filter-city-options');
  const stateOptions = document.getElementById('customers-filter-state-options');
  const searchInput = document.getElementById('customers-search-input');
  const noResultsMessage = document.getElementById('customers-filter-no-results');

  function normalizeValue(value) {
    return String(value || '').trim().toLowerCase();
  }

  function getUniqueOptions(attributeName) {
    const uniqueMap = new Map();

    customerCards.forEach(function (card) {
      const label = String(card.dataset[attributeName] || '').trim();
      const normalized = normalizeValue(label);
      if (!normalized || uniqueMap.has(normalized)) {
        return;
      }

      uniqueMap.set(normalized, label);
    });

    return Array.from(uniqueMap.entries())
      .map(function (entry) {
        return { value: entry[0], label: entry[1] };
      })
      .sort(function (a, b) {
        return a.label.localeCompare(b.label, undefined, { sensitivity: 'base' });
      });
  }

  function renderCheckboxOptions(container, filterName, options, emptyLabel) {
    if (!container) {
      return;
    }

    if (options.length === 0) {
      container.innerHTML = '<p>' + emptyLabel + '</p>';
      return;
    }

    container.innerHTML = options
      .map(function (option, index) {
        const inputId = filterName + '-' + index;
        return (
          '<label for="' + inputId + '">' +
            '<input id="' + inputId + '" type="checkbox" name="' + filterName + '" value="' + option.value + '" /> ' +
            option.label +
          '</label>'
        );
      })
      .join('');
  }

  function getCheckedValues(filterName) {
    return new Set(
      Array.from(document.querySelectorAll('input[name="' + filterName + '"]:checked')).map(function (input) {
        return normalizeValue(input.value);
      })
    );
  }

  function applyFilters() {
    const selectedStatuses = getCheckedValues('filter_status');
    const selectedTypes = getCheckedValues('filter_type');
    const selectedCities = getCheckedValues('filter_city');
    const selectedStates = getCheckedValues('filter_state');
    const query = normalizeValue(searchInput ? searchInput.value : '');

    let visibleCount = 0;

    customerCards.forEach(function (card) {
      const status = normalizeValue(card.dataset.filterStatus);
      const type = normalizeValue(card.dataset.filterType);
      const city = normalizeValue(card.dataset.filterCity);
      const state = normalizeValue(card.dataset.filterState);
      const fullName = normalizeValue(card.dataset.searchName || '');

      const matchesStatus = selectedStatuses.size === 0 || selectedStatuses.has(status);
      const matchesType = selectedTypes.size === 0 || selectedTypes.has(type);
      const matchesCity = selectedCities.size === 0 || selectedCities.has(city);
      const matchesState = selectedStates.size === 0 || selectedStates.has(state);
      const matchesSearch = query === '' || fullName.indexOf(query) !== -1;

      const shouldShow = matchesStatus && matchesType && matchesCity && matchesState && matchesSearch;
      card.style.display = shouldShow ? '' : 'none';
      if (shouldShow) {
        visibleCount += 1;
      }
    });

    if (noResultsMessage) {
      noResultsMessage.hidden = visibleCount !== 0;
    }
  }

  renderCheckboxOptions(statusOptions, 'filter_status', getUniqueOptions('filterStatus'), 'No statuses available.');
  renderCheckboxOptions(typeOptions, 'filter_type', getUniqueOptions('filterType'), 'No types available.');
  renderCheckboxOptions(cityOptions, 'filter_city', getUniqueOptions('filterCity'), 'No cities available.');
  renderCheckboxOptions(stateOptions, 'filter_state', getUniqueOptions('filterState'), 'No states available.');

  [statusOptions, typeOptions, cityOptions, stateOptions].forEach(function (container) {
    if (!container) {
      return;
    }
    container.addEventListener('change', applyFilters);
  });

  if (searchInput) {
    searchInput.addEventListener('input', applyFilters);
    searchInput.addEventListener('search', applyFilters);
  }

  applyFilters();
})();
