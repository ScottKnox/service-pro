document.addEventListener("DOMContentLoaded", () => {
  const roots = document.querySelectorAll("[data-health-filter-root]");
  if (!roots.length) {
    return;
  }

  roots.forEach((root) => {
    const filterButtons = Array.from(root.querySelectorAll("[data-health-filter]"));
    const customerItems = Array.from(root.querySelectorAll("[data-health-customer]"));
    const emptyState = root.querySelector("[data-health-customer-empty]");

    if (!filterButtons.length) {
      return;
    }

    const setFilter = (selected) => {
      filterButtons.forEach((button) => {
        const isActive = button.dataset.healthFilter === selected;
        button.classList.toggle("is-active", isActive);
        button.setAttribute("aria-pressed", isActive ? "true" : "false");
      });

      let visibleCount = 0;
      customerItems.forEach((item) => {
        const condition = item.dataset.healthCustomer;
        const isVisible = selected === "all" || condition === selected;
        item.classList.toggle("is-filtered-out", !isVisible);
        item.setAttribute("aria-hidden", isVisible ? "false" : "true");
        if (isVisible) {
          visibleCount += 1;
        }
      });

      if (emptyState) {
        emptyState.hidden = visibleCount > 0;
      }
    };

    filterButtons.forEach((button) => {
      button.addEventListener("click", () => {
        setFilter(button.dataset.healthFilter || "all");
      });
    });

    setFilter("all");
  });
});
