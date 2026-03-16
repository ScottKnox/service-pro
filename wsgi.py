from app import app

# WSGI servers (gunicorn, uWSGI, mod_wsgi) look for `application` by convention.
application = app


if __name__ == "__main__":
    application.run()
