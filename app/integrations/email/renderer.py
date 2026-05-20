"""Jinja2-backed renderer for transactional email templates.

Templates live as paired `<name>.html` + `<name>.txt` files under
`templates/files/`. Both versions share the same context dict so there's
no risk of html/text drifting with incompatible placeholders.

The OTP email predates this module and uses the inline-f-string pattern
in `templates/email_otp.py`. That one is tiny enough not to need
Jinja2; leaving it in place avoids gratuitous churn."""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES_DIR = Path(__file__).parent / "templates" / "files"


_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_email(name: str, context: dict) -> tuple[str, str]:
    """Render `{name}.html` and `{name}.txt` with the same context.
    Returns `(html, text)`. Raises `jinja2.TemplateNotFound` if either
    half is missing — we treat the pair as a single artifact so tests
    can't accidentally skip the text fallback."""
    html = _env.get_template(f"{name}.html").render(**context)
    text = _env.get_template(f"{name}.txt").render(**context)
    return html, text
