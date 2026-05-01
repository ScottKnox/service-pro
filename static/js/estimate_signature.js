(function () {
  function initializeEstimateSignatureFlow() {
    const modal = document.getElementById('estimate-signature-modal');
    const openButton = document.getElementById('open-estimate-signature-modal');
    const closeButton = document.getElementById('estimate-signature-close-btn');
    const cancelButton = document.getElementById('estimate-signature-cancel-btn');
    const clearButton = document.getElementById('estimate-signature-clear-btn');
    const submitButton = document.getElementById('estimate-signature-submit-btn');
    const form = document.getElementById('public-estimate-accept-form');
    const signatureInput = document.getElementById('estimate-signature-data-url');
    const canvas = document.getElementById('estimate-signature-canvas');

    if (!modal || !openButton || !form || !signatureInput || !canvas || !window.SignaturePad) {
      return;
    }

    let signaturePad = null;

    function resizeCanvas() {
      const ratio = Math.max(window.devicePixelRatio || 1, 1);
      const parentWidth = canvas.parentElement ? canvas.parentElement.clientWidth : canvas.clientWidth;
      const targetWidth = Math.max(1, Math.floor(parentWidth));
      const targetHeight = 260;

      canvas.style.width = targetWidth + 'px';
      canvas.style.height = targetHeight + 'px';
      canvas.width = Math.floor(targetWidth * ratio);
      canvas.height = Math.floor(targetHeight * ratio);

      const context = canvas.getContext('2d');
      context.scale(ratio, ratio);

      if (signaturePad) {
        signaturePad.clear();
      }
    }

    function openModal() {
      modal.classList.add('is-open');
      modal.setAttribute('aria-hidden', 'false');
      if (!signaturePad) {
        signaturePad = new window.SignaturePad(canvas, {
          minWidth: 0.8,
          maxWidth: 2.2,
          penColor: '#1B263B',
          backgroundColor: 'rgba(255,255,255,1)',
        });
      }
      resizeCanvas();
    }

    function closeModal() {
      modal.classList.remove('is-open');
      modal.setAttribute('aria-hidden', 'true');
    }

    openButton.addEventListener('click', openModal);

    if (closeButton) {
      closeButton.addEventListener('click', closeModal);
    }

    if (cancelButton) {
      cancelButton.addEventListener('click', closeModal);
    }

    if (clearButton) {
      clearButton.addEventListener('click', function () {
        if (signaturePad) {
          signaturePad.clear();
        }
      });
    }

    if (submitButton) {
      submitButton.addEventListener('click', function () {
        if (!signaturePad || signaturePad.isEmpty()) {
          alert('Please provide your signature before submitting.');
          return;
        }

        signatureInput.value = signaturePad.toDataURL('image/png');
        form.submit();
      });
    }

    modal.addEventListener('click', function (event) {
      if (event.target === modal) {
        closeModal();
      }
    });

    window.addEventListener('resize', function () {
      if (modal.classList.contains('is-open')) {
        resizeCanvas();
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeEstimateSignatureFlow);
  } else {
    initializeEstimateSignatureFlow();
  }
})();
