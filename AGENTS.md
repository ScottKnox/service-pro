# AI Agent Instructions for Klovent Service-Pro

Klovent Service-Pro is a Flask monolith for HVAC service management: jobs, estimates, invoices, payments, reminders, SMS, and customer/business admin.

## Start Here

Use this setup on Windows:

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
pytest
```

If you need local MongoDB, use a local service or Docker and keep `APP_ENV=local` in `.env`. The app expects `SECRET_KEY`, local Mongo settings, and `NOTIFICATION_LOCAL_BASE_URL` in local mode. Production mode uses `MONGODB_URI` or discrete `MONGODB_*` values plus `NOTIFICATION_BASE_URL`.

## Project Shape

- Entry point: `app.py`
- Configuration: `config.py`
- Mongo access and serialization: `mongo.py`
- Blueprints: `blueprints/`
- Templates: `templates/`
- Static assets: `static/`
- PDF/invoice generation: `invoice_generator.py` and `hvac_report_generator.py`
- Utilities: `utils/`

## Important Patterns

- Use `ensure_connection_or_500()` for DB access and `object_id_or_404()` for ID validation.
- Scheduler work must run inside `app.app_context()`.
- Keep notification links aligned with the configured base URL for the current environment.
- Use `serialize_doc()` before returning Mongo documents in JSON responses.
- Prefer existing helpers and blueprint patterns over introducing new data access paths.

## Testing And Verification

- Primary test runner: `pytest`
- Test infrastructure uses `mongomock` / `mongomock-motor`-style in-memory database behavior where applicable.
- Stripe and Twilio are optional integrations; leave feature flags and secrets unset unless you are testing those flows.
- For payment or reminder changes, verify the relevant job/invoice workflow end-to-end instead of only unit-testing a helper.

## When Editing

- Keep changes small and consistent with the current Flask and Mongo style.
- Avoid reworking unrelated blueprints, templates, or static files.
- Update `.env.example` and this file together when environment expectations change.
