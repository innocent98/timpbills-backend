import sys

from loguru import logger

from app.core.config import settings


class _StdlibStyleLogger:
    """Make loguru accept stdlib ``%``-style log args.

    The codebase logs stdlib-style — ``log.warning("code=%s", code)`` — but raw
    loguru formats with ``{}`` and therefore **silently drops** the positional
    args, printing the literal ``%s``. That is a real observability bug: e.g. a
    VTPass rejection logged as ``code=%s description=%s`` with neither value, so
    ops can't see ``code=027 IP NOT WHITELISTED`` and has to dig on the provider
    dashboard.

    This wrapper pre-formats ``msg % args`` when positional args are present and
    hands loguru a finished string (no args), so nothing is dropped. It is safe
    because the codebase has zero ``{}``-style loguru calls, and a no-arg message
    is passed through by loguru verbatim (braces/%% in the result are fine).
    ``opt(depth=2)`` keeps the record pointing at the real call site
    (file:function:line), not this wrapper.
    """

    def __init__(self, logger):
        self._logger = logger

    def _emit(self, level: str, msg, args: tuple, *, exc: bool = False) -> None:
        if args:
            try:
                msg = str(msg) % args
            except Exception:
                # Never let a bad format string raise from a log call; degrade
                # to appending the args so no information is lost.
                msg = f"{msg} args={args!r}"
        # depth=2: skip _emit + the public method (warning/info/...) so loguru
        # attributes the record to the caller.
        self._logger.opt(depth=2, exception=exc).log(level, "{}", msg)

    def debug(self, msg, *args, **_kw) -> None:
        self._emit("DEBUG", msg, args)

    def info(self, msg, *args, **_kw) -> None:
        self._emit("INFO", msg, args)

    def warning(self, msg, *args, **_kw) -> None:
        self._emit("WARNING", msg, args)

    def error(self, msg, *args, **_kw) -> None:
        self._emit("ERROR", msg, args)

    def exception(self, msg, *args, **_kw) -> None:
        self._emit("ERROR", msg, args, exc=True)

    def critical(self, msg, *args, **_kw) -> None:
        self._emit("CRITICAL", msg, args)


def setup_logging():
    logger.remove()

    log_level = "DEBUG" if settings.ENVIRONMENT == "development" else "INFO"

    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=log_level,
        colorize=True,
    )

    logger.add(
        "logs/app.log",
        rotation="10 MB",
        retention="1 week",
        level=log_level,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    )

    return _StdlibStyleLogger(logger)


log = setup_logging()
