from .admin import bp as admin_bp
from .auth import bp as auth_bp
from .business import bp as business_bp
from .catalog import bp as catalog_bp
from .customers import bp as customers_bp
from .employees import bp as employees_bp
from .jobs import bp as jobs_bp


def register_blueprints(app):
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(catalog_bp)
    app.register_blueprint(business_bp)
    app.register_blueprint(customers_bp)
    app.register_blueprint(employees_bp)
    app.register_blueprint(jobs_bp)
