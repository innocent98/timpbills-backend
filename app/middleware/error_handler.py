from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            error = {
                "code": detail["code"],
                "message": detail.get("message", ""),
                "details": detail.get("details"),
            }
        else:
            error = {"code": "HTTP_ERROR", "message": str(detail), "details": None}
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "data": None,
                "error": error,
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # Pydantic v2 stuffs the original exception instance into each
        # error's ``ctx`` (e.g. the raw ``ValueError`` raised by a
        # ``@field_validator``).  Those instances are not JSON-serializable,
        # so feed the whole list through ``jsonable_encoder`` first.  That
        # coerces unknown objects to their string form, preserving the
        # human-readable error while keeping the response serializable.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Validation failed",
                    "details": jsonable_encoder(exc.errors()),
                },
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    @app.exception_handler(Exception)
    async def fallback_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "An unexpected error occurred",
                    "details": None,
                },
                "request_id": getattr(request.state, "request_id", None),
            },
        )
