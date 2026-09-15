import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.api import api_router
from app.core.config import settings
from app.core.limiter import limiter
from app.core.logger import log
from app.core.sentry_setup import setup_sentry
from app.middleware.error_handler import install_error_handlers
from app.middleware.request_id import RequestIdMiddleware

# Sentry must init before FastAPI so its integrations can wrap ASGI lifecycle.
setup_sentry()


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time

        log.info(
            f"{request.method} {request.url.path} "
            f"completed in {process_time:.4f}s with status {response.status_code}"
        )

        return response


# Swagger UI, ReDoc and the OpenAPI schema are served only where
# settings.docs_enabled is true (local development + tests). On the deployed
# staging and production servers all three are None, so the routes 404 and the
# API surface is not exposed. See Settings.docs_enabled.
_docs_enabled = settings.docs_enabled

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json" if _docs_enabled else None,
    docs_url=f"{settings.API_V1_STR}/docs" if _docs_enabled else None,
    redoc_url=f"{settings.API_V1_STR}/redoc" if _docs_enabled else None,
)

# Attach limiter to app state
app.state.limiter = limiter

# Exception handlers — must be registered before middleware runs
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
install_error_handlers(app)

# Middleware — runs outermost first (LIFO for add_middleware)
# RequestIdMiddleware must run before error handlers can read request_id
app.add_middleware(LoggingMiddleware)
app.add_middleware(RequestIdMiddleware)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API router
app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/")
def root():
    body = {
        "message": f"Welcome to {settings.PROJECT_NAME} API",
        "version": settings.VERSION,
    }
    # Only advertise the docs link where the docs are actually served.
    if _docs_enabled:
        body["docs"] = f"{settings.API_V1_STR}/docs"
    return body


@app.get("/health")
def health_check():
    return {"status": "healthy"}


def start():
    """Entry point for poetry script."""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.SERVER_PORT,
        reload=True if settings.ENVIRONMENT == "development" else False,
    )


if __name__ == "__main__":
    start()
