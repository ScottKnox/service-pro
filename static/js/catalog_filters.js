(function () {
  const filterRoots = document.querySelectorAll('[data-catalog-filter-root]');

  function normalizeValue(value) {
    return String(value || '').trim().toLowerCase();
  }

  function isInteractiveTarget(target) {
    return Boolean(target.closest('a, button, input, textarea, select, label, summary'));
  }

  filterRoots.forEach(function (root) {
    const cards = Array.from(root.querySelectorAll('[data-catalog-card]'));
    const noResultsMessage = root.querySelector('#catalog-filter-no-results');
    const searchInput = root.querySelector('[data-catalog-search-input]');

    cards.forEach(function (card) {
      const cardLinkUrl = card.dataset.cardLinkUrl;
      if (!cardLinkUrl) {
        return;
      }

      card.addEventListener('click', function (event) {
        if (isInteractiveTarget(event.target)) {
          return;
        }

        window.location.assign(cardLinkUrl);
      });

      card.addEventListener('keydown', function (event) {
        if (isInteractiveTarget(event.target)) {
          return;
        }

        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          window.location.assign(cardLinkUrl);
        }
      });
    });

    function getCheckedValues(filterName) {
      return new Set(
        Array.from(root.querySelectorAll('input[name="' + filterName + '"]:checked')).map(function (input) {
          return normalizeValue(input.value);
        })
      );
    }

    function applyFilters() {
      const selectedCategories = getCheckedValues('filter_category');
      const selectedCodes = getCheckedValues('filter_code');
      const selectedManufacturers = getCheckedValues('filter_manufacturer');
      const searchQuery = normalizeValue(searchInput ? searchInput.value : '');
      let visibleCount = 0;

      cards.forEach(function (card) {
        const category = normalizeValue(card.dataset.filterCategory);
        const code = normalizeValue(card.dataset.filterCode);
        const manufacturer = normalizeValue(card.dataset.filterManufacturer);
        const name = normalizeValue(card.dataset.filterName);
        const matchesCategory = selectedCategories.size === 0 || selectedCategories.has(category);
        const matchesCode = selectedCodes.size === 0 || selectedCodes.has(code);
        const matchesManufacturer = selectedManufacturers.size === 0 || selectedManufacturers.has(manufacturer);
        const matchesSearch = searchQuery === '' || name.indexOf(searchQuery) !== -1;
        const shouldShow = matchesCategory && matchesCode && matchesManufacturer && matchesSearch;
        card.style.display = shouldShow ? '' : 'none';
        if (shouldShow) {
          visibleCount += 1;
        }
      });

      if (noResultsMessage) {
        noResultsMessage.hidden = visibleCount !== 0;
      }
    }

    root.querySelectorAll('input[type="checkbox"]').forEach(function (checkbox) {
      checkbox.addEventListener('change', applyFilters);
    });

    if (searchInput) {
      searchInput.addEventListener('input', applyFilters);
      searchInput.addEventListener('search', applyFilters);
    }

    applyFilters();
  });
})();