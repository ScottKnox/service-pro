from bson import ObjectId


def make_estimate_doc(**overrides):
    document = {
        "_id": ObjectId(),
        "customer_id": ObjectId(),
        "total_amount": 0,
    }
    document.update(overrides)
    return document


def seed_estimate(db, **overrides):
    estimate = make_estimate_doc(**overrides)
    db.estimates.insert_one(estimate)
    return estimate
