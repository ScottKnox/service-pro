import importlib
import os

import mongomock
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient


@pytest.fixture(scope="session")
def app_module():
    os.environ.setdefault("SECRET_KEY", "test-secret-key")
    os.environ.setdefault("MONGODB_DB_NAME", "service_pro_test")
    return importlib.import_module("app")


@pytest.fixture
def mongo_db():
    client = mongomock.MongoClient()
    db = client["service_pro_test"]
    yield db
    client.drop_database("service_pro_test")


@pytest.fixture
def async_mongo_client():
    # Kept for the upcoming FastAPI migration where API tests become async.
    return AsyncMongoMockClient()


@pytest.fixture
def test_app(app_module, mongo_db, monkeypatch):
    flask_app = app_module.app
    flask_app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
    )

    monkeypatch.setattr(app_module, "ensure_connection_or_500", lambda: mongo_db)

    customers_module = importlib.import_module("blueprints.customers")
    monkeypatch.setattr(customers_module, "ensure_connection_or_500", lambda: mongo_db)

    return flask_app


@pytest.fixture
def authed_client(test_app):
    with test_app.test_client() as client:
        with client.session_transaction() as sess:
            sess["employee_id"] = str(ObjectId())
            sess["employee_name"] = "Integration Tester"
            sess["employee_position"] = "admin"
        yield client
