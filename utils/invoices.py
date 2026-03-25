"""Invoice collection and management utilities."""


def collect_invoice_items(db):
    """Collect all invoices from jobs in the database."""
    invoice_items = []
    jobs_with_invoices = db.jobs.find(
        {"invoices.0": {"$exists": True}},
        {"customer_name": 1, "scheduled_date": 1, "scheduled_time": 1, "invoices": 1, "total": 1, "job_type": 1},
    ).sort([("scheduled_date", -1), ("_id", -1)])

    for job in jobs_with_invoices:
        customer_name = job.get("customer_name", "Unknown Customer")
        scheduled_date = job.get("scheduled_date", "")
        scheduled_time = job.get("scheduled_time", "")
        total = job.get("total", "$0.00")
        job_type = job.get("job_type", "Service")
        job_id = str(job.get("_id", ""))

        for invoice in reversed(job.get("invoices", [])):
            invoice_items.append(
                {
                    "invoice_number": invoice.get("invoice_number", "Invoice"),
                    "file_path": invoice.get("file_path", "#"),
                    "customer_name": customer_name,
                    "scheduled_date": scheduled_date,
                    "scheduled_time": scheduled_time,
                    "total": total,
                    "job_type": job_type,
                    "job_id": job_id,
                }
            )

    return invoice_items
