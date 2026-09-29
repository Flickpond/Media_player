"""Explicit target authorization for non-Locust load helpers."""

import os
from uuid import UUID

LOCAL_URL = "http://127.0.0.1:13000"
PRODUCTION_URL = "https://flickpond.com"


def checked_target():
    mode = os.environ.get("LOAD_MODE", "local")
    base_url = os.environ.get("LOAD_BASE_URL", "")
    if mode == "local" and base_url == LOCAL_URL:
        return base_url
    if mode == "production" and base_url == PRODUCTION_URL:
        run_id = os.environ.get("LOAD_RUN_ID", "")
        try:
            UUID(run_id)
        except ValueError as exc:
            raise RuntimeError("production requires a UUID LOAD_RUN_ID") from exc
        if os.environ.get("LOAD_CONFIRM") == f"flickpond-production:{run_id}":
            return base_url
    raise RuntimeError("target or production confirmation is not authorized")