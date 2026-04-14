from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
import time

from app.core.config import settings
from app.core.logger import log
from app.core.limiter import limiter
from app.api.v1.api import api_router
from app.middleware.error_handler import install_error_handlers
from app.middleware.request_id import RequestIdMiddleware


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


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url=f"{settings.API_V1_STR}/docs",
    redoc_url=f"{settings.API_V1_STR}/redoc",
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
    return {
        "message": f"Welcome to {settings.PROJECT_NAME} API",
        "version": settings.VERSION,
        "docs": f"{settings.API_V1_STR}/docs",
    }


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
