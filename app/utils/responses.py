from typing import Any


def success(data: Any, request_id: str | None = None) -> dict:
    return {"success": True, "data": data, "error": None, "request_id": request_id}
