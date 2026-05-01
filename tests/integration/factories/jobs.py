from bson import ObjectId


def make_job_doc(**overrides):
    document = {
        "_id": ObjectId(),
        "customer_id": ObjectId(),
        "status": "Pending",
    }
    document.update(overrides)
    return document


def seed_job(db, **overrides):
    job = make_job_doc(**overrides)
    db.jobs.insert_one(job)
    return job
