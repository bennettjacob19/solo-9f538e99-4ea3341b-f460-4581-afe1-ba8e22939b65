"""Local entry point: ``python wsgi.py`` (threaded dev server).

Docker/gunicorn uses ``wsgi:app`` as well.
"""
from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True, debug=False)
