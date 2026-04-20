(function () {
  const formatTimeToAmPm = (timeString) => {
    const match = timeString.match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
    if (!match) {
      return null;
    }

    const hours24 = parseInt(match[1], 10);
    const minutes = match[2];
    if (Number.isNaN(hours24) || hours24 < 0 || hours24 > 23) {
      return null;
    }

    const period = hours24 >= 12 ? 'PM' : 'AM';
    const hours12 = hours24 % 12 || 12;
    return `${hours12}:${minutes} ${period}`;
  };

  const formatUtcDateTimeToLocalAmPm = (value) => {
    const match = value.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})$/);
    if (!match) {
      return null;
    }

    const month = parseInt(match[1], 10) - 1;
    const day = parseInt(match[2], 10);
    const year = parseInt(match[3], 10);
    const hours = parseInt(match[4], 10);
    const minutes = parseInt(match[5], 10);
    const seconds = parseInt(match[6], 10);

    const utcDate = new Date(Date.UTC(year, month, day, hours, minutes, seconds));
    if (Number.isNaN(utcDate.getTime())) {
      return null;
    }

    const localMonth = String(utcDate.getMonth() + 1).padStart(2, '0');
    const localDay = String(utcDate.getDate()).padStart(2, '0');
    const localYear = utcDate.getFullYear();
    const localHours24 = utcDate.getHours();
    const localMinutes = String(utcDate.getMinutes()).padStart(2, '0');
    const period = localHours24 >= 12 ? 'PM' : 'AM';
    const localHours12 = localHours24 % 12 || 12;

    return `${localMonth}/${localDay}/${localYear} ${localHours12}:${localMinutes} ${period}`;
  };

  const formatJobTimeDisplays = () => {
    const scheduledTimeElement = document.getElementById('scheduled-time-display');
    if (scheduledTimeElement) {
      const rawScheduledTime = scheduledTimeElement.textContent.trim();
      if (rawScheduledTime && rawScheduledTime !== 'N/A') {
        const formattedScheduledTime = formatTimeToAmPm(rawScheduledTime);
        if (formattedScheduledTime) {
          scheduledTimeElement.textContent = formattedScheduledTime;
        }
      }
    }

    ['date-started-display', 'date-completed-display'].forEach((fieldId) => {
      const element = document.getElementById(fieldId);
      if (!element) {
        return;
      }

      const rawValue = element.textContent.trim();
      if (!rawValue || rawValue === 'N/A') {
        return;
      }

      const formattedValue = formatUtcDateTimeToLocalAmPm(rawValue);
      if (formattedValue) {
        element.textContent = formattedValue;
      }
    });
  };

  // Format time fields on page load.
  formatJobTimeDisplays();

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
  let currentEmailType = 'estimate';

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
      currentEmailType = emailType;

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
          if (currentEmailType === 'estimate') {
            window.location.reload();
          }
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
