(() => {
  const toggleButton = document.getElementById("mobile-nav-toggle");
  const navigation = document.getElementById("header-navigation");

  if (!toggleButton || !navigation) {
    return;
  }

  function closeMenu() {
    navigation.classList.remove("is-open");
    toggleButton.setAttribute("aria-expanded", "false");
    toggleButton.setAttribute("aria-label", "Open navigation menu");
  }

  function openMenu() {
    navigation.classList.add("is-open");
    toggleButton.setAttribute("aria-expanded", "true");
    toggleButton.setAttribute("aria-label", "Close navigation menu");
  }

  toggleButton.addEventListener("click", () => {
    if (navigation.classList.contains("is-open")) {
      closeMenu();
      return;
    }

    openMenu();
  });

  navigation.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => {
      if (window.innerWidth <= 900) {
        closeMenu();
      }
    });
  });

  window.addEventListener("resize", () => {
    if (window.innerWidth > 900) {
      closeMenu();
    }
  });
})();
