(() => {
  const pageLinks = document.querySelectorAll("#home-jobs-pages a");

  pageLinks.forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      pageLinks.forEach((item) => item.classList.remove("is-active"));
      link.classList.add("is-active");
    });
  });
})();

(() => {
  const monthLabel = document.getElementById("home-calendar-month-label");
  const daysContainer = document.getElementById("home-calendar-days");
  const prevButton = document.getElementById("home-calendar-prev");
  const nextButton = document.getElementById("home-calendar-next");

  if (!monthLabel || !daysContainer || !prevButton || !nextButton) {
    return;
  }

  const today = new Date();
  const selectedDays = new Set([
    dayKey(today.getFullYear(), today.getMonth(), today.getDate()),
  ]);
  const visibleMonth = new Date();
  visibleMonth.setDate(1);

  function dayKey(year, month, day) {
    return `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  }

  function todayKey() {
    return dayKey(today.getFullYear(), today.getMonth(), today.getDate());
  }

  function yesterdayKey() {
    const d = new Date(today);
    d.setDate(today.getDate() - 1);
    return dayKey(d.getFullYear(), d.getMonth(), d.getDate());
  }

  function tomorrowKey() {
    const d = new Date(today);
    d.setDate(today.getDate() + 1);
    return dayKey(d.getFullYear(), d.getMonth(), d.getDate());
  }

  function updateHeadline(key) {
    const headline = document.querySelector(".home-jobs-column h2");
    if (!headline) return;
    if (!key || key === todayKey()) {
      headline.textContent = "Today's Jobs";
    } else if (key === yesterdayKey()) {
      headline.textContent = "Yesterday's Jobs";
    } else if (key === tomorrowKey()) {
      headline.textContent = "Tomorrow's Jobs";
    } else {
      const [y, m, d] = key.split("-").map(Number);
      const formatted = new Date(y, m - 1, d).toLocaleDateString("en-US", {
        month: "long",
        day: "numeric",
        year: "numeric",
      });
      headline.textContent = `${formatted} Jobs`;
    }
  }

  function renderCalendar() {
    const year = visibleMonth.getFullYear();
    const month = visibleMonth.getMonth();

    monthLabel.textContent = visibleMonth.toLocaleDateString("en-US", {
      month: "long",
      year: "numeric",
    });

    daysContainer.innerHTML = "";

    const firstDayOfMonth = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();

    for (let i = 0; i < firstDayOfMonth; i += 1) {
      const filler = document.createElement("button");
      filler.type = "button";
      filler.className = "home-calendar-day is-empty";
      filler.disabled = true;
      filler.setAttribute("aria-hidden", "true");
      daysContainer.appendChild(filler);
    }

    for (let day = 1; day <= daysInMonth; day += 1) {
      const key = dayKey(year, month, day);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "home-calendar-day";
      button.textContent = String(day);

      if (selectedDays.has(key)) {
        button.classList.add("is-selected");
      }

      button.addEventListener("click", () => {
        if (selectedDays.has(key)) {
          selectedDays.delete(key);
          button.classList.remove("is-selected");
          updateHeadline(null);
        } else {
          selectedDays.clear();
          daysContainer.querySelectorAll(".home-calendar-day.is-selected").forEach((el) => {
            el.classList.remove("is-selected");
          });
          selectedDays.add(key);
          button.classList.add("is-selected");
          updateHeadline(key);
        }
      });

      daysContainer.appendChild(button);
    }
  }

  prevButton.addEventListener("click", () => {
    visibleMonth.setMonth(visibleMonth.getMonth() - 1);
    renderCalendar();
  });

  nextButton.addEventListener("click", () => {
    visibleMonth.setMonth(visibleMonth.getMonth() + 1);
    renderCalendar();
  });

  renderCalendar();
  updateHeadline(todayKey());
})();
