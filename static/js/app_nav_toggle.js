(() => {
  const toggleButton = document.getElementById("app-nav-toggle");
  const homeSidebar = document.getElementById("home-sidebar");

  if (!toggleButton || !homeSidebar) {
    return;
  }

  function setExpandedState(isExpanded) {
    toggleButton.setAttribute("aria-expanded", isExpanded ? "true" : "false");
    toggleButton.setAttribute("aria-label", isExpanded ? "Hide navigation" : "Show navigation");
  }

  function syncDefaultState() {
    if (window.innerWidth <= 900) {
      homeSidebar.classList.add("is-links-hidden");
      setExpandedState(false);
      return;
    }

    homeSidebar.classList.remove("is-links-hidden");
    setExpandedState(true);
  }

  syncDefaultState();

  toggleButton.addEventListener("click", () => {
    const isLinksHidden = homeSidebar.classList.toggle("is-links-hidden");
    setExpandedState(!isLinksHidden);
  });

  window.addEventListener("resize", syncDefaultState);
})();
