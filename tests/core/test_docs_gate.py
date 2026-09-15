"""API docs (Swagger UI / ReDoc / OpenAPI) are served only outside the
deployed staging and production environments. See Settings.docs_enabled and
app/main.py."""
from app.core.config import settings
from app.main import app


def test_docs_disabled_only_on_staging_and_production():
    for env in ("staging", "production", "STAGING", " Production "):
        assert settings.model_copy(update={"ENVIRONMENT": env}).docs_enabled is False
    for env in ("development", "local", "test", ""):
        assert settings.model_copy(update={"ENVIRONMENT": env}).docs_enabled is True


def test_app_doc_routes_track_docs_enabled():
    # main.py wires all three doc URLs to settings.docs_enabled, so they are
    # present together in local/test and absent together on staging/prod.
    expected = settings.docs_enabled
    assert (app.docs_url is not None) is expected
    assert (app.redoc_url is not None) is expected
    assert (app.openapi_url is not None) is expected
