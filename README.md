# Klovent

## Run locally

1. Create a virtual environment:
	python3 -m venv .venv
2. Activate the virtual environment:
	source .venv/bin/activate
3. Install dependencies:
	python -m pip install -r requirements.txt
4. Create a local env file:
	cp .env.example .env

5. Set MongoDB connection settings in `.env`:

	Option A: provide a full URI (recommended for MongoDB Atlas)
	MONGODB_URI="mongodb://username:password@host:27017/?authSource=admin"
	MONGODB_DB_NAME="service_pro"

	Option B: provide host and auth variables separately
	MONGODB_HOST="localhost"
	MONGODB_PORT="27017"
	MONGODB_USERNAME="your_username"
	MONGODB_PASSWORD="your_password"
	MONGODB_AUTH_SOURCE="admin"
	MONGODB_DB_NAME="service_pro"

	If your username or password includes special characters, they are URL-encoded by the app when building the URI.
6. Start the Flask app:
	python app.py
7. Open:
	http://127.0.0.1:5000/

## MongoDB collections

The app reads and writes these collections in the configured database:

- customers
- jobs
- services

## Stripe test payments (local)

Use this checklist to test invoice card payments in Stripe test mode only.

Install Stripe: winget install Stripe.StripeCLI

1. Add Stripe test variables to `.env`:
	STRIPE_SECRET_KEY=sk_test_your_secret_key
	STRIPE_PUBLISHABLE_KEY=pk_test_your_publishable_key
	STRIPE_WEBHOOK_SECRET=whsec_from_stripe_cli
	STRIPE_CURRENCY=usd
	STRIPE_COUNTRY=US
	STRIPE_PLATFORM_FEE_PERCENT=0

2. Restart the app after editing `.env`:
	python app.py

3. In another terminal, start Stripe webhook forwarding:
	stripe login
	stripe listen --forward-to http://127.0.0.1:5000/payments/stripe/webhook --forward-connect-to http://127.0.0.1:5000/payments/stripe/webhook

4. Copy the `whsec_...` value printed by `stripe listen` into `STRIPE_WEBHOOK_SECRET` in `.env`, then restart the app again.

5. Open a tokenized customer invoice link and click:
	Make Payment -> Pay By Card

6. Use Stripe test card details:
	Card number: 4242 4242 4242 4242
	Expiry: any future date
	CVC: any 3 digits
	ZIP: any value

7. Verify results:
	Stripe Dashboard (Test mode): payment succeeds and `checkout.session.completed` event is delivered.
	Klovent: invoice shows paid state, job status moves to Paid, customer balance is reduced.

Notes:
- Keep Stripe Dashboard in Test mode while validating.
- Never place `sk_live_...` or `pk_live_...` keys in local `.env`.
- For Stripe Connect destination charges, include `--forward-connect-to` in the Stripe CLI command so connected account events reach your webhook.

## Stripe production cutover checklist

Use this checklist when moving from test mode to live processing.

1. Confirm your platform Stripe account is activated for live payments.

2. Confirm Stripe Connect onboarding is working for at least one HVAC business in live mode.

3. Create a live webhook endpoint in Stripe Dashboard:
	Endpoint URL: https://your-domain.com/payments/stripe/webhook
	Events:
	- checkout.session.completed
	- checkout.session.async_payment_succeeded

4. Update production environment variables:
	STRIPE_SECRET_KEY=sk_live_your_secret_key
	STRIPE_PUBLISHABLE_KEY=pk_live_your_publishable_key
	STRIPE_WEBHOOK_SECRET=whsec_from_live_webhook_endpoint
	STRIPE_CURRENCY=usd
	STRIPE_COUNTRY=US
	STRIPE_PLATFORM_FEE_PERCENT=0

5. Restart/redeploy the service so new environment values are loaded.

6. Verify production data safety:
	- Back up production database.
	- Confirm no test Stripe keys remain in production environment.
	- Confirm logging does not expose secrets.

7. Run a small live payment smoke test with a low-value invoice.

## Stripe post-launch verification checklist

After live cutover, verify this flow end-to-end:

1. Business has Stripe Connect Ready = Yes in Business Profile.

2. Customer can open tokenized invoice and start card checkout.

3. Payment succeeds in Stripe live dashboard.

4. Webhook deliveries for checkout events return 2xx.

5. Klovent updates correctly:
	- invoice shows paid state
	- job status is Paid
	- customer balance is reduced

6. Platform fee behavior is correct for your configured `STRIPE_PLATFORM_FEE_PERCENT`.

7. Email links and paid state are correct on both staff and customer invoice views.

If any step fails, check webhook delivery logs in Stripe first, then application logs for webhook signature or session retrieval errors.
