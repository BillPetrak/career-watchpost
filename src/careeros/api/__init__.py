"""CareerOS REST API (FastAPI)."""

from careeros.api.main import app, create_app

__all__: tuple[str, ...] = ("app", "create_app")
