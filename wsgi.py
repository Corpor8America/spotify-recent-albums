"""WSGI entry point for gunicorn: ``gunicorn wsgi:app``.

Kept separate from app.py so that importing the app module stays
side-effect-free (the scheduler only starts when this file is loaded by
an actual server).
"""

from app import create_app

app = create_app()
