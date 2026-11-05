// Placeholder site behavior - no dark mode logic yet. Adding dark mode
// (toggle, data-theme attribute, localStorage persistence) is the task.
(function () {
  document.addEventListener("DOMContentLoaded", function () {
    var yearEl = document.getElementById("year");
    if (yearEl) {
      yearEl.textContent = String(new Date().getFullYear());
    }

    var navToggle = document.getElementById("nav-toggle");
    var siteNav = document.getElementById("site-nav");
    if (navToggle && siteNav) {
      navToggle.addEventListener("click", function () {
        var isOpen = siteNav.classList.toggle("open");
        navToggle.setAttribute("aria-expanded", String(isOpen));
      });
    }

    var contactForm = document.getElementById("contact-form");
    if (contactForm) {
      contactForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var status = document.getElementById("form-status");
        if (status) {
          status.textContent = "Thanks! This demo form doesn't actually send anything.";
        }
      });
    }
  });
})();
