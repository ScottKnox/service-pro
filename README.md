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
