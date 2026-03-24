(function () {
  const modal = document.getElementById('email-estimate-modal');
  const emailButtons = document.querySelectorAll('.estimate-email-btn');
  const closeBtn = document.getElementById('modal-close-btn');
  const cancelBtn = document.getElementById('modal-cancel-btn');
  const emailForm = document.getElementById('email-estimate-form');
  const recipientInput = document.getElementById('email-recipient');
  const subjectInput = document.getElementById('email-subject');
  const bodyInput = document.getElementById('email-body');

  if (!modal || !emailForm || !recipientInput || !subjectInput || !bodyInput) {
    return;
  }

  const sendUrl = emailForm.dataset.sendUrl || '';
  const quoteTemplate = emailForm.dataset.quoteTemplate || '';
  const invoiceTemplate = emailForm.dataset.invoiceTemplate || '';
  let currentEstimateFile = '';

  function openModal() {
    modal.classList.add('is-open');
  }

  function closeModal() {
    modal.classList.remove('is-open');
  }

  emailButtons.forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      const estimateTitle = this.getAttribute('data-estimate-title');
      const estimateFile = this.getAttribute('data-estimate-file');
      const customerEmail = this.getAttribute('data-customer-email');
      const customerName = this.getAttribute('data-customer-name').trim();
      const emailType = this.getAttribute('data-email-type') || 'estimate';

      recipientInput.value = customerEmail;
      if (emailType === 'invoice') {
        subjectInput.value = `Invoice: ${estimateTitle}`;
        bodyInput.value = invoiceTemplate || `Hi ${customerName},\n\nPlease find attached your invoice.\n\nPlease let us know if you have any questions.\n\nBest regards`;
      } else {
        subjectInput.value = `Estimate: ${estimateTitle}`;
        bodyInput.value = quoteTemplate || `Hi ${customerName},\n\nPlease find attached your estimate.\n\nPlease let us know if you have any questions.\n\nBest regards`;
      }

      currentEstimateFile = estimateFile;
      openModal();
    });
  });

  if (closeBtn) {
    closeBtn.addEventListener('click', closeModal);
  }

  if (cancelBtn) {
    cancelBtn.addEventListener('click', closeModal);
  }

  modal.addEventListener('click', function (e) {
    if (e.target === modal) {
      closeModal();
    }
  });

  emailForm.addEventListener('submit', function (e) {
    e.preventDefault();

    const recipient = recipientInput.value;
    const subject = subjectInput.value;
    const body = bodyInput.value;
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';

    fetch(sendUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken,
      },
      body: JSON.stringify({
        recipient_email: recipient,
        subject: subject,
        body: body,
        estimate_file: currentEstimateFile,
      }),
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (data.success) {
          alert('Email sent successfully!');
          closeModal();
          emailForm.reset();
        } else {
          alert('Error sending email: ' + (data.error || 'Unknown error'));
        }
      })
      .catch(function (error) {
        console.error('Error:', error);
        alert('Error sending email: ' + error);
      });
  });
})();
