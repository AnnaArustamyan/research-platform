"""ASGI entry point: `uvicorn trackrecord.api.main:app`."""

from trackrecord.api.app import create_app

app = create_app()
