(function () {
  // Convert UTC times to local timezone
  const convertTimeStringsToLocal = () => {
    const timeFieldIds = [
      'scheduled-time-display',
      'date-started-display',
      'date-completed-display',
    ];

    timeFieldIds.forEach((fieldId) => {
      const element = document.getElementById(fieldId);
      if (!element) return;

      const timeString = element.textContent.trim();
      if (!timeString || timeString === 'N/A') return;

      const localTime = convertUTCToLocal(timeString);
      if (localTime) {
        element.textContent = localTime;
      }
    });
  };

  const convertUTCToLocal = (timeString) => {
    try {
      const parts = timeString.match(/(\d{1,2})\/(\d{1,2})\/(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})/);
      if (!parts) return null;

      const month = parseInt(parts[1], 10) - 1;
      const day = parseInt(parts[2], 10);
      const year = parseInt(parts[3], 10);
      const hours = parseInt(parts[4], 10);
      const minutes = parseInt(parts[5], 10);
      const seconds = parseInt(parts[6], 10);

      const utcDate = new Date(Date.UTC(year, month, day, hours, minutes, seconds));
      const localDate = new Date(utcDate.toLocaleString('en-US', { timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone }));

      const localMonth = String(localDate.getMonth() + 1).padStart(2, '0');
      const localDay = String(localDate.getDate()).padStart(2, '0');
      const localYear = localDate.getFullYear();
      const localHours = String(localDate.getHours()).padStart(2, '0');
      const localMinutes = String(localDate.getMinutes()).padStart(2, '0');
      const localSeconds = String(localDate.getSeconds()).padStart(2, '0');

      return `${localMonth}/${localDay}/${localYear} ${localHours}:${localMinutes}:${localSeconds}`;
    } catch (error) {
      console.error('Error converting time:', error);
      return null;
    }
  };

  // Convert times on page load
  convertTimeStringsToLocal();

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

    const sendBtn = emailForm.querySelector('.modal-send-btn');
    const originalBtnText = sendBtn ? sendBtn.textContent : '';
    if (sendBtn) {
      sendBtn.disabled = true;
      sendBtn.textContent = 'Sending...';
    }

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
        if (sendBtn) {
          sendBtn.disabled = false;
          sendBtn.textContent = originalBtnText;
        }
        if (data.success) {
          alert('Email sent successfully!');
          closeModal();
          emailForm.reset();
        } else {
          alert('Error sending email: ' + (data.error || 'Unknown error'));
        }
      })
      .catch(function (error) {
        if (sendBtn) {
          sendBtn.disabled = false;
          sendBtn.textContent = originalBtnText;
        }
        console.error('Error:', error);
        alert('Error sending email: ' + error);
      });
  });
})();
