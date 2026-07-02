"""Uvicorn entry-point shim — the app lives in seizr/web/.

    uvicorn webapp:app --host 0.0.0.0 --port 8000
"""

from seizr.web import app  # noqa: F401
