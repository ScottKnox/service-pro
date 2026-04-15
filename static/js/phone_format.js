(function () {
  function formatPhone(value) {
    const rawValue = (value || "").trim();
    if (!rawValue) {
      return "";
    }

    const extensionMatch = rawValue.match(/(?:ext\.?|x)\s*(\d{1,6})\s*$/i);
    const extensionDigits = extensionMatch ? extensionMatch[1] : "";
    const mainValue = extensionMatch ? rawValue.slice(0, extensionMatch.index).trim() : rawValue;

    let digits = mainValue.replace(/\D/g, "");

    let countryCode = "";
    if (digits.length > 10 && digits.startsWith("1")) {
      countryCode = "1-";
      digits = digits.slice(1);
    }

    digits = digits.slice(0, 10);

    let formattedMain = "";

    if (digits.length <= 3) {
      formattedMain = countryCode + digits;
    } else if (digits.length <= 7) {
      // 7-digit local US format: XXX-XXXX
      formattedMain = countryCode + digits.slice(0, 3) + "-" + digits.slice(3);
    } else {
      // 10-digit US format: XXX-XXX-XXXX
      formattedMain = countryCode + digits.slice(0, 3) + "-" + digits.slice(3, 6) + "-" + digits.slice(6);
    }

    if (!extensionDigits) {
      return formattedMain;
    }

    return formattedMain ? formattedMain + " x" + extensionDigits : "x" + extensionDigits;
  }

  function attachPhoneFormatter(input) {
    if (!input || input.dataset.phoneFormatterAttached === "true") {
      return;
    }

    input.dataset.phoneFormatterAttached = "true";
    input.setAttribute("maxlength", "21");

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
