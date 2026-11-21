"""Production entry point: `gunicorn wsgi:app`."""

from buydips.web import create_app

app = create_app()
