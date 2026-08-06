from app.platform.routes import templates
from app.web import berlin_datetime


def test_platform_templates_register_berlin_datetime_filter():
    assert templates.env.filters["berlin"] is berlin_datetime
    assert templates.get_template("platform_dashboard.html") is not None
