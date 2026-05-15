# Scheduler & Payment System Deep Dive

**Focus:** Critical patterns for invoice reminders, background scheduler, payment webhooks, and notification delivery.  
**Audience:** Agents working on billing, reminders, payments, or notification systems.

---

## The Invoice Reminder System (Architecture)

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. INVOICE CREATED                                              │
│    → Job marked with invoice record                             │
│    → schedule_invoice_reminders_for_invoice() called            │
│    → Creates 3 automatic reminder docs:                         │
│       • Day 1 (sequence 1)                                      │
│       • Day 7 (sequence 2)                                      │
│       • Day 14 (sequence 3)                                     │
└─────────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│ 2. SCHEDULER TICK (Every 60 minutes)                            │
│    → APScheduler calls _run_invoice_reminder_scheduler_tick()   │
│    → Inside app.app_context()                                   │
│    → Calls process_due_invoice_reminders()                      │
│    → Query: { status: "Created", scheduled_for: {$lte: now} }   │
│    → For each due reminder:                                     │
│       • Build email/SMS body via _build_invoice_reminder_message()
│       • Get payment link via _build_notification_url()          │
│       • Send via Flask-Mail or Twilio                           │
│       • Update: status → "Sent", automatic_sent_at → now        │
└─────────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│ 3. PAYMENT RECEIVED                                             │
│    → Customer clicks payment link → Stripe checkout             │
│    → Stripe webhook → POST /payments/stripe/webhook             │
│    → Verify signature with STRIPE_WEBHOOK_SECRET                │
│    → process_stripe_checkout_completed()                        │
│    → Updates: job.status → "Paid", invoice.status → "Paid"      │
│    → Creates final reminder doc: status: "Paid" (for tracking)  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Reminder Document Schema

### Automatic Reminder (Scheduled)
```python
{
    "_id": ObjectId(...),
    "job_id": "12345",  # Reference to job
    "invoice_number": "INV-001",
    "reminder_type": "automatic",  # ← Critical: distinguishes from manual
    "automatic_sequence_number": 1,  # 1=1-day, 2=7-day, 3=14-day (NO sequence for manual!)
    "status": "Created",  # Transitions: Created → Sent → Paid
    "scheduled_for": "2025-01-15T18:00:00",  # When to send (ISO format)
    "automatic_sent_at": None,  # Set when actually sent
    "business_id": "...",
    "customer_id": "...",
    "created_at": "2025-01-01T10:00:00"
}
```

### Manual Reminder (Sent Immediately)
```python
{
    "_id": ObjectId(...),
    "job_id": "12345",
    "invoice_number": "INV-001",
    "reminder_type": "manual",  # ← User clicked "Send Reminder" button
    # NO automatic_sequence_number for manual!
    "status": "scheduled",
    "scheduled_for": "2025-01-15T18:30:00",  # Set to ~now
    "manual_sent_at": "2025-01-15T18:32:00",  # User-triggered, sent immediately
    "business_id": "...",
    "customer_id": "...",
    "created_at": "2025-01-15T18:30:00"
}
```

**Key Distinction:** 
- `automatic_sequence_number` exists ONLY for automatic reminders
- Manual reminders use `reminder_type: "manual"` and `manual_sent_at`
- Query filter: `process_due_invoice_reminders()` queries only `reminder_type: "automatic"`

---

## Critical Code Locations

### 1. Scheduler Setup (app.py)

**Lines ~817–850:**
```python
def _invoice_reminder_scheduler_enabled():
    """Check if scheduler should run"""
    # Disabled if: SMS_FEATURES_ENABLED=false, debug mode, tests, or explicitly set

def _invoice_reminder_scheduler_interval_minutes():
    """Read INVOICE_REMINDER_SCHEDULER_INTERVAL_MINUTES, default 60"""

def _run_invoice_reminder_scheduler_tick():
    """Called every N minutes; MUST use app.app_context()"""
    with app.app_context():  # ← CRITICAL: Without this, url_for() fails
        db = ensure_connection_or_500()
        processed_count = process_due_invoice_reminders(db=db, batch_size=200)

def _start_invoice_reminder_scheduler():
    """Initialize APScheduler on app startup"""
    # Creates BackgroundScheduler with daemon=True (won't block exit)
    # Adds job: _run_invoice_reminder_scheduler_tick every N minutes
```

### 2. Background Processor (blueprints/jobs.py, line ~1509)

**Function:** `process_due_invoice_reminders(db=None, batch_size=100)`

```python
def process_due_invoice_reminders(db=None, batch_size=100):
    """
    CRITICAL: Process automatic reminders only (reminder_type == "automatic")
    
    This is called from the scheduler tick inside app.app_context()
    It queries MongoDB for due reminders and sends them.
    """
    # ✅ CORRECT: Check if db is None explicitly
    if db is None:
        db = ensure_connection_or_500()
    
    # ✅ CORRECT: Use $convert to handle both BSON dates and ISO strings
    # (In case scheduled_for was manually edited as string in DB)
    query = {
        "status": "Created",
        "reminder_type": "automatic",  # ← Filter automatic only
        "$expr": {
            "$lte": [
                {"$convert": {"input": "$scheduled_for", "to": "date", "onError": None}},
                datetime.utcnow()
            ]
        }
    }
    
    reminders = db.invoice_reminders.find(query).limit(batch_size)
    
    for reminder_doc in reminders:
        _send_single_invoice_reminder(db, reminder_doc)
    
    return len(list(reminders))
```

**Why `$convert`?**
- Scheduler may have edited `scheduled_for` manually as ISO string: `"2025-01-15T18:00:00"`
- MongoDB date comparison requires BSON date objects
- `$convert` ensures both types match

**Why filter `reminder_type == "automatic"`?**
- Manual reminders are sent immediately (not via scheduler)
- Prevents duplicate sends if manual reminder stored with old code

---

### 3. URL Building for Notifications (blueprints/jobs.py, line ~1049)

**Function:** `_build_notification_url(endpoint, external=False, **route_kwargs)`

```python
def _build_notification_url(endpoint, external=False, **route_kwargs):
    """
    Build URL for notification emails/SMS
    
    CRITICAL: Called from scheduler context (no request!)
    Solution: Create temp request context if NOTIFICATION_BASE_URL set
    
    Flow:
    1. If NOTIFICATION_BASE_URL env var exists: use it + create temp context
    2. Else if SERVER_NAME in config: use url_for() directly
    3. Else: return relative URL
    """
    base_url = os.getenv("NOTIFICATION_BASE_URL")  # e.g., "https://app.klovent.com"
    
    if base_url:
        # Create temporary request context to avoid scheduler crashes
        with app.test_request_context(base_url=base_url):
            return url_for(endpoint, _external=external, **route_kwargs)
    
    # Fallback: try existing request context
    try:
        return url_for(endpoint, _external=external, **route_kwargs)
    except RuntimeError:
        # Outside request context and no NOTIFICATION_BASE_URL
        return f"/{endpoint}"
```

**Why temp request context?**
- Scheduler runs in background thread (no HTTP request active)
- `url_for(_external=True)` requires either:
  - Active request context, OR
  - Flask app config `SERVER_NAME` set
- Creating temp context + using `NOTIFICATION_BASE_URL` is safer than relying on config

**Common bug:** Forgetting `NOTIFICATION_BASE_URL` env var
```bash
# ❌ WRONG: Scheduler crashes with "Unable to build URLs..."
python app.py  # NOTIFICATION_BASE_URL not set

# ✅ CORRECT: Set env var first
export NOTIFICATION_BASE_URL=https://app.klovent.com
python app.py
```

---

### 4. Sending Single Reminder (blueprints/jobs.py, line ~1383)

**Function:** `_send_single_invoice_reminder(db, reminder_doc)`

```python
def _send_single_invoice_reminder(db, reminder_doc):
    """
    Send email and/or SMS for single due reminder
    
    Steps:
    1. Get job + invoice + customer details
    2. Build message body (splits on reminder_type + sequence_number)
    3. Build payment link via _build_notification_url()
    4. Send email + SMS if configured
    5. Update reminder: status → "Sent", automatic_sent_at → now
    """
    job_id = reminder_doc.get("job_id")
    reminder_type = reminder_doc.get("reminder_type", "automatic")
    reminder_number = reminder_doc.get("automatic_sequence_number", 1)
    
    job = db.jobs.find_one({"_id": ObjectId(job_id)})
    customer = db.customers.find_one({"_id": ObjectId(reminder_doc.get("customer_id"))})
    
    # Build message (splits on reminder_type)
    message_body = _build_invoice_reminder_message(
        reminder_type=reminder_type,
        reminder_number=reminder_number,
        job=job,
        customer=customer
    )
    
    # Build payment link (uses _build_notification_url)
    payment_link = _build_notification_url(
        "jobs.create_invoice_checkout_session",
        job_id=job_id,
        invoice_number=reminder_doc.get("invoice_number")
    )
    
    # Send email
    if sms_features_enabled():
        send_email(
            to=customer.get("email"),
            subject=f"Invoice {reminder_doc.get('invoice_number')} - Payment Due",
            body=f"{message_body}\n\nPay now: {payment_link}"
        )
        
        # Send SMS
        send_sms_via_twilio(
            to_number=customer.get("phone"),
            message_body=f"{message_body} Pay: {payment_link}"
        )
    
    # Update reminder status
    db.invoice_reminders.update_one(
        {"_id": reminder_doc["_id"]},
        {
            "$set": {
                "status": "Sent",
                "automatic_sent_at": datetime.utcnow().isoformat()
            }
        }
    )
```

---

### 5. Message Copy (blueprints/jobs.py, line ~1260)

**Function:** `_build_invoice_reminder_message(reminder_type, reminder_number, ...)`

```python
def _build_invoice_reminder_message(reminder_type, reminder_number, job, customer):
    """
    Build SMS/email body
    
    CRITICAL DISTINCTION:
    - Automatic reminders (reminder_type="automatic"): Include "X days past due"
    - Manual reminders (reminder_type="manual"): Generic "invoice unpaid" (no sequential copy)
    """
    
    invoice_number = ...
    amount_due = ...
    
    if reminder_type == "automatic":
        # Automatic: sequential copy based on reminder_number (1, 2, 3)
        days_past_due_mapping = {
            1: "1 day",
            2: "7 days",
            3: "14 days"
        }
        days_text = days_past_due_mapping.get(reminder_number, "")
        
        return f"""Invoice {invoice_number} is {days_text} past due.
Amount due: ${amount_due}
Customer: {customer.get('company_name')}"""
    
    else:  # reminder_type == "manual"
        # Manual: generic (no sequential copy, no "past due" text)
        return f"""Invoice {invoice_number} payment reminder.
Amount due: ${amount_due}
Customer: {customer.get('company_name')}"""
```

**Why split?** In earlier versions, manual reminders inherited "7 days past due" text from sequence numbering, creating confusion. Now split by `reminder_type`.

---

## Stripe Webhook Integration

### Webhook Handler (app.py, line ~4405)

```python
@app.route("/payments/stripe/webhook", methods=["POST"])
@csrf.exempt  # ← Stripe posts externally, not from our forms
def stripe_webhook():
    """
    Handle Stripe checkout completion webhook
    
    1. Extract + verify signature
    2. Extract checkout session metadata (job_id, invoice_number)
    3. Call process_stripe_checkout_completed()
    """
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature")
    
    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            os.getenv("STRIPE_WEBHOOK_SECRET")
        )
    except Exception as exc:
        app.logger.error(f"Webhook signature verification failed: {exc}")
        return {"error": "Invalid signature"}, 400
    
    # Handle completion events
    if event.get("type") in {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded"
    }:
        checkout_session = event.get("data", {}).get("object", {})
        process_stripe_checkout_completed(db, checkout_session)
        return {"success": True}, 200
```

### Payment Processing (blueprints/jobs.py, line ~4605)

```python
def process_stripe_checkout_completed(db, checkout_session):
    """
    Update job/invoice/customer when payment received
    
    Steps:
    1. Extract metadata (job_id, invoice_number)
    2. Find job + invoice
    3. Update statuses: job → "Paid", invoice.status → "Paid"
    4. Update customer balance
    5. Create final reminder doc: status "Paid" (for tracking)
    """
    
    metadata = checkout_session.get("metadata", {})
    job_id = metadata.get("job_id")
    invoice_number = metadata.get("invoice_number")
    
    job = db.jobs.find_one({"_id": ObjectId(job_id)})
    invoice_entry = next(
        (inv for inv in job.get("invoices", [])
         if inv.get("invoice_number") == invoice_number),
        None
    )
    
    if not invoice_entry:
        app.logger.error(f"Invoice {invoice_number} not found in job {job_id}")
        return
    
    # Update job status
    db.jobs.update_one(
        {"_id": job["_id"]},
        {"$set": {"status": "Paid"}}
    )
    
    # Update invoice status
    db.jobs.update_one(
        {"_id": job["_id"], "invoices.invoice_number": invoice_number},
        {"$set": {"invoices.$.status": "Paid"}}
    )
    
    # Update customer balance
    db.customers.update_one(
        {"_id": ObjectId(job.get("customer_id"))},
        {"$inc": {"balance": -float(invoice_entry.get("total", 0))}}
    )
    
    # Create final reminder doc for tracking
    db.invoice_reminders.insert_one({
        "job_id": job_id,
        "invoice_number": invoice_number,
        "reminder_type": "automatic",
        "status": "Paid",
        "scheduled_for": datetime.utcnow().isoformat(),
        "automatic_sent_at": datetime.utcnow().isoformat(),
        "business_id": str(job.get("business_id")),
        "customer_id": str(job.get("customer_id")),
        "created_at": datetime.utcnow().isoformat()
    })
```

---

## Testing the Scheduler

### Local Webhook Testing

```bash
# Terminal 1: Start app
python app.py

# Terminal 2: Start Stripe webhook forwarding
stripe login
stripe listen --forward-to http://127.0.0.1:5000/payments/stripe/webhook
# Shows: whsec_xxxxx (copy to .env as STRIPE_WEBHOOK_SECRET)

# Terminal 3: Trigger a payment in the UI
# 1. Navigate to an invoice
# 2. Click "Make Payment"
# 3. Use test card: 4242 4242 4242 4242, exp: any future date, CVC: any 3 digits
# 4. Complete checkout
# → Webhook fires → job status updates to "Paid"
```

### Scheduler Testing (Fast Interval)

```bash
# Speed up scheduler for testing (check every 1 minute instead of 60)
INVOICE_REMINDER_SCHEDULER_INTERVAL_MINUTES=1 python app.py

# Then manually trigger by editing MongoDB:
# db.invoice_reminders.updateOne(
#   {"_id": ObjectId("...")},
#   {"$set": {"scheduled_for": ISODate("2025-01-01T00:00:00Z")}}  # Past time
# )

# Watch logs for "Invoice reminder scheduler tick" + reminder sent
```

---

## Common Errors & Fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `Unable to build URLs outside an active request...` | Scheduler calling `url_for()` without context | Set `NOTIFICATION_BASE_URL` env var |
| `Database objects do not implement truth value testing` | Code uses `if db:` on Mongo connection | Use `if db is None:` instead |
| `scheduled_for` doesn't match | Mixed date formats (string vs BSON) | Use `$convert` in aggregation query |
| Duplicate reminders sent | Query includes both auto + manual | Filter: `reminder_type: "automatic"` only |
| SMS not sending | `SMS_FEATURES_ENABLED` not set | Set to `true` + verify Twilio credentials |
| Webhook doesn't verify | Wrong `STRIPE_WEBHOOK_SECRET` | Copy from `stripe listen` output, restart app |

---

## Invoice Reminder Indexes (MongoDB)

**Query optimization (mongo.py, `_ensure_invoice_reminder_indexes()`):**

```python
db.invoice_reminders.create_index([("status", 1), ("scheduled_for", 1)])
db.invoice_reminders.create_index([("job_id", 1), ("reminder_type", 1)])
db.invoice_reminders.create_index([("automatic_sequence_number", 1)])
```

**Query pattern:** Scheduler queries `{status: "Created", scheduled_for: {$lte: now}}`  
→ Needs index on `(status, scheduled_for)` for efficiency

---

## Invoice Model Evolution (In Code)

```python
# Current invoice document (inside job.invoices array)
{
    "invoice_number": "INV-001",
    "status": "Sent",                    # NEW: Created | Sent | Paid (not job.status)
    "total": 1500.00,
    "date_sent": "2025-01-15T18:05:00",  # ISO format (internal)
    "date_sent_utc": 1736967900,         # Unix timestamp
    "created_at": "2025-01-15T10:00:00",
    ...
}

# Template variable (rendered as MM/DD/YYYY HH:MM:SS)
invoice_sent_display = "01/15/2025 18:05:00"  # From _resolve_invoice_sent_display()
```

---

## Debugging Checklist

- [ ] `NOTIFICATION_BASE_URL` set? (Required for scheduler)
- [ ] `STRIPE_WEBHOOK_SECRET` correct? (Required for payment webhook)
- [ ] `SMS_FEATURES_ENABLED=true`? (Required for SMS auto-send)
- [ ] Scheduler enabled? (`INVOICE_REMINDER_SCHEDULER_ENABLED=true`)
- [ ] Scheduler interval reasonable? (Default 60 min; use 1 min for testing)
- [ ] Reminder status filters correct? (Automatic should filter `reminder_type`)
- [ ] Payment link building inside `_build_notification_url()`? (Uses temp context)
- [ ] Test card used for Stripe? (4242 4242 4242 4242)
- [ ] Logs checked for exceptions? (Look for "Invoice reminder scheduler tick" + error traces)

