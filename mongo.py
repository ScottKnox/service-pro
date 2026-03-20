import os
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from bson import ObjectId
from dotenv import load_dotenv
from flask import abort
from pymongo import MongoClient
from pymongo.errors import PyMongoError

load_dotenv()

MONGODB_HOST = os.getenv("MONGODB_HOST", "localhost")
MONGODB_PORT = os.getenv("MONGODB_PORT", "27017")
MONGODB_USERNAME = os.getenv("MONGODB_USERNAME", "")
MONGODB_PASSWORD = os.getenv("MONGODB_PASSWORD", "")
MONGODB_AUTH_SOURCE = os.getenv("MONGODB_AUTH_SOURCE", "admin")
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME")

_mongo_client = None


def _inject_credentials_into_uri(uri: str) -> str:
    if not (MONGODB_USERNAME and MONGODB_PASSWORD):
        return uri

    parts = urlsplit(uri)
    if "@" in parts.netloc:
        return uri

    username = quote_plus(MONGODB_USERNAME)
    password = quote_plus(MONGODB_PASSWORD)
    netloc = f"{username}:{password}@{parts.netloc}"

    query = parts.query
    if MONGODB_AUTH_SOURCE and parts.scheme == "mongodb":
        query_items = dict(parse_qsl(query, keep_blank_values=True))
        query_items.setdefault("authSource", MONGODB_AUTH_SOURCE)
        query = urlencode(query_items)

    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def build_mongodb_uri() -> str:
    if MONGODB_URI:
        return _inject_credentials_into_uri(MONGODB_URI)

    if MONGODB_HOST.startswith("mongodb://") or MONGODB_HOST.startswith("mongodb+srv://"):
        return _inject_credentials_into_uri(MONGODB_HOST)

    credentials = ""
    if MONGODB_USERNAME and MONGODB_PASSWORD:
        username = quote_plus(MONGODB_USERNAME)
        password = quote_plus(MONGODB_PASSWORD)
        credentials = f"{username}:{password}@"

    uri = f"mongodb://{credentials}{MONGODB_HOST}:{MONGODB_PORT}"
    if credentials:
        uri = f"{uri}/?authSource={quote_plus(MONGODB_AUTH_SOURCE)}"
    return uri


def get_db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(build_mongodb_uri(), serverSelectionTimeoutMS=3000)
    return _mongo_client[MONGODB_DB_NAME]


def object_id_or_404(value: str) -> ObjectId:
    if not ObjectId.is_valid(value):
        abort(404)
    return ObjectId(value)


def serialize_doc(doc):
    if not doc:
        return None
    serialized = {**doc, "_id": str(doc["_id"])}
    return serialized


def ensure_connection_or_500():
    try:
        db = get_db()
        db.command("ping")
        return db
    except PyMongoError as exc:
        abort(500, description=f"MongoDB connection failed: {exc}")
