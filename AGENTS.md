# AI Agent Instructions for Klovent Service-Pro

**Platform:** HVAC service management SaaS for small businesses (job scheduling, invoicing, payments, SMS notifications).

---

## Quick Start

### Local Development

```bash
# Setup (Windows)
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt

# Configuration
cp .env.example .env  # Update MONGODB_URI, STRIPE keys, TWILIO credentials

# Run
python app.py  # Starts Flask on http://localhost:5000 + background scheduler
pytest         # Run tests with mongomock
```

### Key Environment Variables

| Variable | Purpose | Example |
|----------|---------|---------|
| `MONGODB_URI` | Database connection | `mongodb+srv://user:pass@cluster...` |
| `STRIPE_SECRET_KEY` | Payment processing | `sk_test_...` |
| `STRIPE_WEBHOOK_SECRET` | Webhook verification | `whsec_...` |
| `TWILIO_ACCOUNT_SID` | SMS provider | (from Twilio console) |
| `TWILIO_AUTH_TOKEN` | SMS authentication | (from Twilio console) |
| `SMS_FEATURES_ENABLED` | Enable SMS notifications | `true` or `false` |
| `NOTIFICATION_BASE_URL` | Base URL for all email/SMS links | `https://app.klovent.com` |
| `INVOICE_REMINDER_SCHEDULER_INTERVAL_MINUTES` | Scheduler frequency (default 60) | `60` |

---

## Architecture at a Glance

```
app.py                          # Entry point, Flask config, scheduler setup
├── blueprints/
│   ├── jobs.py               # ⭐ Core: jobs, invoices, reminders, payments
│   ├── customers.py          # Customer CRUD, properties, HVAC systems
│   ├── employees.py          # Team management
│   ├── auth.py               # Login, session
│   ├── business.py           # Company config
│   ├── catalog.py            # Service/part/labor catalog
│   └── admin.py              # Subscriptions, reporting
├── mongo.py                   # MongoDB connection, serialization
├── invoice_generator.py       # ReportLab PDF generation
├── utils/
│   ├── invoices.py           # Invoice calculations
│   ├── currency.py           # Currency formatting
│   └── formatters.py         # Date/phone formatting
├── templates/                # Jinja2 templates
└── static/                   # CSS, JS
```

**Database:**
- MongoDB collections: `employees`, `customers`, `jobs`, `invoices`, `businesses`, `subscriptions`, `invoice_reminders`, etc.
- All ObjectId and datetime serialized to strings in JSON responses (`serialize_doc()`)

**Deployment:**
- WSGI: `wsgi.py` → `gunicorn --workers 4 wsgi:application`
- Scheduler: Runs in-process via APScheduler (background thread)

---

## Critical Patterns to Know

### 1. Error Handling & DB Access

```python
# ✅ Always use these helpers to avoid crashes
db = ensure_connection_or_500()              # Connection with error handling
object_id = object_id_or_404(string_id)     # Convert + validate or 404
```

### 2. Invoice Reminders (Scheduler-Based)

**Flow:**
1. Invoice created → `schedule_invoice_reminders_for_invoice()` creates reminder docs
   - Automatic reminders: 1, 7, 14 days past due (from `INVOICE_REMINDER_DAY_OFFSETS`)
   - Each has `automatic_sequence_number` (1/2/3) + `status: "Created"`
2. Scheduler runs every 60 min (configurable):
   - Queries reminders where `scheduled_for <= now` and `status == "Created"`
   - Calls `_send_single_invoice_reminder()` → sends email/SMS via `_build_notification_url()`
   - Sets `status: "Sent"` + `automatic_sent_at` timestamp
3. Manual reminders via UI: `send_invoice_reminder_manually()` 
   - Stored as `reminder_type: "manual"` (no sequence number)
   - Sent immediately, not via scheduler

**Key helpers:**
- `process_due_invoice_reminders(db, batch_size=100)` – Background processor
- `_build_notification_url(endpoint, **route_kwargs)` – Creates payment links for emails/SMS
- `_build_invoice_reminder_message(reminder_type, reminder_number, ...)` – SMS/email body

**Common issue:** `process_due_invoice_reminders()` must run inside `app.app_context()` or `url_for()` fails. Scheduler tick handles this:

```python
def _run_invoice_reminder_scheduler_tick():
    with app.app_context():  # ← Required
        db = ensure_connection_or_500()
        process_due_invoice_reminders(db=db, batch_size=200)
```

### 3. Payment Processing (Stripe)

**Flow:**
1. Invoice paid → Stripe webhook (`POST /payments/stripe/webhook`)
2. Handler calls `process_stripe_checkout_completed(db, checkout_session)`
3. Updates:
   - `job.status` → "Paid"
   - `job.invoices[].status` → "Paid"
   - `customer.balance` updated
   - Creates invoice reminder with `status: "Paid"` (to track payment)

**Test locally:**
```bash
# Terminal 1: Start app
python app.py

# Terminal 2: Forward Stripe webhooks
stripe login
stripe listen --forward-to http://127.0.0.1:5000/payments/stripe/webhook
# Copy STRIPE_WEBHOOK_SECRET output, set in .env

# Terminal 3: Trigger payment
# Navigate to invoice → "Make Payment" → Test card 4242 4242 4242 4242
```

### 4. SMS Notifications

**Auto-triggered on job status change:**
```python
# When job.status = "En Route", automatically sends SMS to customer
_send_en_route_sms_notification(db, job_doc)
```

**URL Building (Important for Scheduler):**
- All notification URLs go through `_build_notification_url(endpoint, **kwargs)`
- This creates a temporary Flask request context to avoid scheduler crashes
- Requires `NOTIFICATION_BASE_URL` env var for scheduler to work

### 5. Invoice Status Lifecycle

```
Created → Sent → Paid
         (email/SMS sent)  (payment received)
```

- Invoice doc has separate `status` field (not job.status)
- Display template uses `invoice.status` for status badge
- Timestamp tracking: `invoice.date_sent` (ISO format), `invoice_sent_display` (MM/DD/YYYY HH:MM:SS)

---

## Common Pitfalls & Prevention

| Pitfall | Prevention |
|---------|-----------|
| **Scheduler context error** | Always wrap scheduler tick in `app.app_context()` |
| **Invalid ObjectId crash** | Use `object_id_or_404()` helper instead of raw `ObjectId()` |
| **Missing `NOTIFICATION_BASE_URL`** | Set this env var; without it, scheduler can't build notification links |
| **Mixed date formats** | Use `_parse_event_datetime()` helper (supports 6+ formats) |
| **Twilio not configured** | Check `SMS_FEATURES_ENABLED=true` + credentials set before SMS triggers |
| **Session data lost** | Always stringify ObjectId before storing in session: `str(doc["_id"])` |
| **Connection pooling in tests** | Reset `_mongo_client = None` in test setup (see `conftest.py` pattern) |
| **Template context bloat** | `home()` route builds excessive context; split into separate endpoints |

---

## Key Files by Task

| Task | File | Function/Pattern |
|------|------|-----------------|
| Add invoice reminder logic | `blueprints/jobs.py` | `schedule_invoice_reminders_for_invoice()` (line ~1311) |
| Debug scheduler | `app.py` | `_run_invoice_reminder_scheduler_tick()` (line ~838) |
| Handle payment webhook | `blueprints/jobs.py` | `process_stripe_checkout_completed()` (line ~4405) |
| Send SMS | `blueprints/jobs.py` | `_send_en_route_sms_notification()` (line ~1600) |
| Format dates | `utils/formatters.py` | `_parse_event_datetime()` |
| Serialize DB docs | `mongo.py` | `serialize_doc()` |
| Build notification URLs | `blueprints/jobs.py` | `_build_notification_url()` (line ~1049) |
| Template rendering | `templates/` | Jinja2 filters: `| currency`, `| format_phone` |

---

## Testing Strategy

- **Unit/Integration:** `pytest` + `mongomock` (in-memory MongoDB)
- **Stripe payments:** Use test card `4242 4242 4242 4242` + Stripe CLI webhook forwarding
- **Scheduler:** Disable in tests via `PYTEST_CURRENT_TEST` env check
- **Feature flags:** `SMS_FEATURES_ENABLED` can disable Twilio calls for testing

---

## Agent Workflow Tips

### When Adding a Feature

1. **Identify the job domain** → Find blueprint (`jobs.py`, `customers.py`, etc.)
2. **Check for pattern match** → Look for similar functionality (e.g., SMS auto-trigger on status change)
3. **Verify DB access** → Use `ensure_connection_or_500()` and `object_id_or_404()`
4. **Handle optional features** → Check feature flags (`SMS_FEATURES_ENABLED`, `INVOICE_REMINDER_SCHEDULER_ENABLED`)
5. **Test with scheduler** → Wrap scheduler-related code in `app.app_context()`
6. **Verify notification URLs** → Use `_build_notification_url()` for email/SMS links

### When Debugging

1. **Scheduler failure?** → Check `NOTIFICATION_BASE_URL` env var is set
2. **404 or invalid ID?** → Validate ObjectId with `ObjectId.is_valid(string_id)`
3. **Date parsing issue?** → Use `_parse_event_datetime()` helper (handles multiple formats)
4. **SMS not sending?** → Verify `SMS_FEATURES_ENABLED=true` + Twilio credentials
5. **Payment not processed?** → Check Stripe webhook signature + `STRIPE_WEBHOOK_SECRET`

---

## Useful Commands

```bash
# Run tests
pytest tests/integration/ -v

# Run specific test
pytest tests/integration/test_jobs.py::test_create_job -v

# Check MongoDB connection
python -c "from mongo import get_db; db = get_db(); print(db.command('ping'))"

# Restart scheduler with faster interval (for testing)
INVOICE_REMINDER_SCHEDULER_INTERVAL_MINUTES=1 python app.py

# Debug scheduled reminders
# Set INVOICE_REMINDER_SCHEDULER_INTERVAL_MINUTES=1, monitor logs for "Invoice reminder scheduler tick"
```

---

## Quick Reference: Invoice Reminder Fields

```python
# Automatic reminder document
{
    "_id": ObjectId(...),
    "job_id": "...",
    "reminder_type": "automatic",              # vs "manual"
    "automatic_sequence_number": 1,            # 1=1-day, 2=7-day, 3=14-day
    "status": "Created",                       # Created → Sent → Paid
    "scheduled_for": "2025-01-15T18:00:00",   # When to send
    "automatic_sent_at": "2025-01-15T18:05:00" # When actually sent
}

# Manual reminder document
{
    "_id": ObjectId(...),
    "job_id": "...",
    "reminder_type": "manual",
    "status": "scheduled",
    "scheduled_for": "...",
    "manual_sent_at": "..."
}
```

---

## Related Documentation

- **Stripe integration details:** See webhook handler in `blueprints/jobs.py` around line 4405
- **Twilio SMS patterns:** See `_send_en_route_sms_notification()` in `blueprints/jobs.py` around line 1600
- **MongoDB schema validation:** See `ensure_collection_validators()` in `mongo.py`
- **Test setup:** See `conftest.py` patterns in `tests/` directory
- **Front-end context:** See `static/js/` for client-side form handling and API interactions

---

**Last Updated:** 2025-05-15  
**For Klovent Engineers:** Use this as your north star when navigating code changes or onboarding AI agents.
