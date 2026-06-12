(function () {
  function initBusinessProfileNavSpy() {
    var sidebarNav = document.querySelector(".resource-business .business-settings-sidebar-nav");
    if (!sidebarNav) {
      return;
    }

    var links = Array.prototype.slice.call(sidebarNav.querySelectorAll('a[href^="#"]'));
    if (!links.length) {
      return;
    }

    var sections = [];
    var linkBySectionId = {};

    links.forEach(function (link) {
      var targetId = link.getAttribute("href").slice(1);
      if (!targetId) {
        return;
      }

      var section = document.getElementById(targetId);
      if (!section) {
        return;
      }

      sections.push(section);
      linkBySectionId[targetId] = link;
    });

    if (!sections.length) {
      return;
    }

    var visibleSectionTops = {};
    var activeId = "";

    function setActiveById(nextId) {
      if (!nextId || !linkBySectionId[nextId] || nextId === activeId) {
        return;
      }

      activeId = nextId;
      links.forEach(function (link) {
        link.classList.toggle("is-active", linkBySectionId[nextId] === link);
      });
    }

    function setActiveFromScroll() {
      var candidateIds = Object.keys(visibleSectionTops);
      var nextId = "";

      if (candidateIds.length) {
        candidateIds.sort(function (a, b) {
          return Math.abs(visibleSectionTops[a]) - Math.abs(visibleSectionTops[b]);
        });
        nextId = candidateIds[0];
      } else {
        var offsetY = window.scrollY + 180;
        sections.forEach(function (section) {
          if (section.offsetTop <= offsetY) {
            nextId = section.id;
          }
        });
        if (!nextId) {
          nextId = sections[0].id;
        }
      }

      setActiveById(nextId);
    }

    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          var sectionId = entry.target.id;
          if (entry.isIntersecting) {
            visibleSectionTops[sectionId] = entry.boundingClientRect.top;
          } else {
            delete visibleSectionTops[sectionId];
          }
        });

        setActiveFromScroll();
      },
      {
        root: null,
        rootMargin: "-20% 0px -65% 0px",
        threshold: [0, 0.1, 0.25, 0.5, 1]
      }
    );

    sections.forEach(function (section) {
      observer.observe(section);
    });

    links.forEach(function (link) {
      link.addEventListener("click", function () {
        var targetId = link.getAttribute("href").slice(1);
        setActiveById(targetId);
      });
    });

    if (window.location.hash) {
      var hashId = window.location.hash.slice(1);
      if (linkBySectionId[hashId]) {
        setActiveById(hashId);
      }
    } else {
      setActiveById(sections[0].id);
    }

    window.addEventListener("hashchange", function () {
      var hashId = window.location.hash.slice(1);
      if (linkBySectionId[hashId]) {
        setActiveById(hashId);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", initBusinessProfileNavSpy);
})();
