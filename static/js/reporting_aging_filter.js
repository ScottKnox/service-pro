document.addEventListener("DOMContentLoaded", () => {
  const roots = document.querySelectorAll("[data-aging-filter-root]");
  if (!roots.length) {
    return;
  }

  roots.forEach((root) => {
    const filterButtons = Array.from(root.querySelectorAll("[data-aging-filter]"));
    const agingCards = Array.from(root.querySelectorAll("[data-aging-bucket]"));
    const receivableItems = Array.from(root.querySelectorAll("[data-aging-receivable]"));
    const receivableEmptyState = root.querySelector("[data-aging-receivable-empty]");

    if (!filterButtons.length || !agingCards.length) {
      return;
    }

    const setFilter = (selectedSeverity) => {
      filterButtons.forEach((button) => {
        const isActive = button.dataset.agingFilter === selectedSeverity;
        button.classList.toggle("is-active", isActive);
        button.setAttribute("aria-pressed", isActive ? "true" : "false");
      });

      agingCards.forEach((card) => {
        const severity = card.dataset.agingBucket;
        const isVisible = selectedSeverity === "all" || severity === selectedSeverity;
        card.classList.toggle("is-filtered-out", !isVisible);
        card.setAttribute("aria-hidden", isVisible ? "false" : "true");
      });

      if (receivableItems.length) {
        let visibleReceivablesCount = 0;
        receivableItems.forEach((item) => {
          const severity = item.dataset.agingReceivable;
          const isVisible = selectedSeverity === "all" || severity === selectedSeverity;
          item.classList.toggle("is-filtered-out", !isVisible);
          item.setAttribute("aria-hidden", isVisible ? "false" : "true");
          if (isVisible) {
            visibleReceivablesCount += 1;
          }
        });

        if (receivableEmptyState) {
          receivableEmptyState.hidden = visibleReceivablesCount > 0;
        }
      }
    };

    filterButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const selectedSeverity = button.dataset.agingFilter || "all";
        setFilter(selectedSeverity);
      });
    });

    setFilter("all");
  });
});
