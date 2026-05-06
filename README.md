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
