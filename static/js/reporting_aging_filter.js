document.addEventListener("DOMContentLoaded", () => {
  const roots = document.querySelectorAll("[data-aging-filter-root]");
  if (!roots.length) {
    return;
  }

  roots.forEach((root) => {
    const filterButtons = Array.from(root.querySelectorAll("[data-aging-filter]"));
    const agingCards = Array.from(root.querySelectorAll("[data-aging-bucket]"));
    const receivableItems = Array.from(root.querySelectorAll("[data-aging-receivable]"));
    const receivableList = root.querySelector("[data-aging-receivable-list]");
    const receivableEmptyState = root.querySelector("[data-aging-receivable-empty]");
    const paginationControls = root.querySelector("[data-aging-pagination-controls]");
    const paginationSummary = root.querySelector("[data-aging-pagination-summary]");
    const prevButton = root.querySelector("[data-aging-page-prev]");
    const nextButton = root.querySelector("[data-aging-page-next]");
    const pageNumbers = root.querySelector("[data-aging-page-numbers]");
    const pageSize = 8;
    let currentPage = 1;
    let activeSeverity = "all";

    if (!filterButtons.length || !agingCards.length) {
      return;
    }

    const sortedVisibleReceivables = (selectedSeverity) => {
      return receivableItems
        .filter((item) => selectedSeverity === "all" || item.dataset.agingReceivable === selectedSeverity)
        .sort((a, b) => {
          const amountA = Number.parseFloat(a.dataset.balanceDue || "0") || 0;
          const amountB = Number.parseFloat(b.dataset.balanceDue || "0") || 0;
          return amountB - amountA;
        });
    };

    const renderPagination = (visibleItems) => {
      if (!receivableItems.length || !receivableList) {
        return;
      }

      const totalItems = visibleItems.length;
      const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
      if (currentPage > totalPages) {
        currentPage = totalPages;
      }

      receivableItems.forEach((item) => {
        item.classList.add("is-filtered-out");
        item.setAttribute("aria-hidden", "true");
      });

      visibleItems.forEach((item) => receivableList.appendChild(item));

      const start = (currentPage - 1) * pageSize;
      const end = start + pageSize;
      const pageItems = visibleItems.slice(start, end);

      pageItems.forEach((item) => {
        item.classList.remove("is-filtered-out");
        item.setAttribute("aria-hidden", "false");
      });

      if (receivableEmptyState) {
        receivableEmptyState.hidden = totalItems > 0;
      }

      if (paginationControls) {
        paginationControls.hidden = totalItems <= pageSize;
      }
      if (paginationSummary) {
        if (!totalItems) {
          paginationSummary.textContent = "No receivables in this bucket";
        } else {
          paginationSummary.textContent = `Showing ${start + 1}-${Math.min(end, totalItems)} of ${totalItems}`;
        }
      }
      if (prevButton) {
        prevButton.disabled = currentPage <= 1;
      }
      if (nextButton) {
        nextButton.disabled = currentPage >= totalPages;
      }

      if (pageNumbers) {
        pageNumbers.innerHTML = "";

        const visiblePages = [];
        if (totalPages <= 7) {
          for (let page = 1; page <= totalPages; page += 1) {
            visiblePages.push(page);
          }
        } else {
          visiblePages.push(1);

          const windowStart = Math.max(2, currentPage - 1);
          const windowEnd = Math.min(totalPages - 1, currentPage + 1);

          if (windowStart > 2) {
            visiblePages.push("ellipsis-left");
          }

          for (let page = windowStart; page <= windowEnd; page += 1) {
            visiblePages.push(page);
          }

          if (windowEnd < totalPages - 1) {
            visiblePages.push("ellipsis-right");
          }

          visiblePages.push(totalPages);
        }

        visiblePages.forEach((entry) => {
          if (typeof entry !== "number") {
            const ellipsis = document.createElement("span");
            ellipsis.className = "reporting-jobs-page-link";
            ellipsis.textContent = "...";
            ellipsis.setAttribute("aria-hidden", "true");
            pageNumbers.appendChild(ellipsis);
            return;
          }

          const pageBtn = document.createElement("button");
          pageBtn.type = "button";
          pageBtn.className = "reporting-jobs-page-link" + (entry === currentPage ? " is-active" : "");
          pageBtn.textContent = String(entry);
          pageBtn.setAttribute("aria-label", `Go to page ${entry}`);
          pageBtn.addEventListener("click", () => {
            currentPage = entry;
            renderPagination(sortedVisibleReceivables(activeSeverity));
          });
          pageNumbers.appendChild(pageBtn);
        });
      }
    };

    const setFilter = (selectedSeverity) => {
      activeSeverity = selectedSeverity;
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
        renderPagination(sortedVisibleReceivables(selectedSeverity));
      }
    };

    if (prevButton) {
      prevButton.addEventListener("click", () => {
        if (currentPage > 1) {
          currentPage -= 1;
          renderPagination(sortedVisibleReceivables(activeSeverity));
        }
      });
    }

    if (nextButton) {
      nextButton.addEventListener("click", () => {
        const visibleCount = sortedVisibleReceivables(activeSeverity).length;
        const totalPages = Math.max(1, Math.ceil(visibleCount / pageSize));
        if (currentPage < totalPages) {
          currentPage += 1;
          renderPagination(sortedVisibleReceivables(activeSeverity));
        }
      });
    }

    filterButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const selectedSeverity = button.dataset.agingFilter || "all";
        currentPage = 1;
        setFilter(selectedSeverity);
      });
    });

    setFilter("all");
  });
});
