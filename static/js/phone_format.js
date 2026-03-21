(function () {
  function formatPhone(value) {
    const digits = (value || "").replace(/\D/g, "").slice(0, 10);

    if (digits.length <= 3) {
      return digits;
    }

    if (digits.length <= 6) {
      return digits.slice(0, 3) + "-" + digits.slice(3);
    }

    return digits.slice(0, 3) + "-" + digits.slice(3, 6) + "-" + digits.slice(6);
  }

  function attachPhoneFormatter(input) {
    if (!input || input.dataset.phoneFormatterAttached === "true") {
      return;
    }

    input.dataset.phoneFormatterAttached = "true";
    input.setAttribute("maxlength", "12");

    input.addEventListener("input", function () {
      this.value = formatPhone(this.value);
    });

    // Normalize any prefilled value.
    input.value = formatPhone(input.value);
  }

  function initPhoneFormatting() {
    const phoneInputs = document.querySelectorAll('input[type="tel"]');
    phoneInputs.forEach(attachPhoneFormatter);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initPhoneFormatting);
  } else {
    initPhoneFormatting();
  }
})();
