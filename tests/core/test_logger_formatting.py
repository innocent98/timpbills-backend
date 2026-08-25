"""The app logs stdlib %-style but `log` is loguru (which formats with {}).

Without the _StdlibStyleLogger wrapper, loguru drops the positional args and
prints the literal `%s` — the bug that hid a VTPass `code=027 IP NOT WHITELISTED`
rejection behind `code=%s description=%s`. These lock in that:
  * %-style args are substituted into the emitted message;
  * a no-arg message with braces / %% is passed through verbatim (no crash);
  * the record still points at the real call site, not the wrapper.
"""
from loguru import logger

from app.core.logger import log


def _capture(fn):
    """Run fn() while capturing loguru messages + the first record dict."""
    messages: list[str] = []
    records: list[dict] = []

    def sink(message):
        messages.append(str(message))
        records.append(message.record)

    sink_id = logger.add(sink, level="DEBUG", format="{message}")
    try:
        fn()
    finally:
        logger.remove(sink_id)
    return messages, records


def test_percent_args_are_substituted():
    msgs, _ = _capture(
        lambda: log.warning(
            "vtpass: purchase failed code=%s description=%s", "027", "IP NOT WHITELISTED"
        )
    )
    joined = "".join(msgs)
    assert "code=027 description=IP NOT WHITELISTED" in joined
    assert "%s" not in joined


def test_no_arg_message_with_braces_and_percent_is_verbatim():
    msgs, _ = _capture(lambda: log.info('payload={"code": "027"} at 100% done'))
    joined = "".join(msgs)
    assert 'payload={"code": "027"} at 100% done' in joined


def test_info_error_and_bad_format_do_not_raise():
    # A format string with more placeholders than args must not raise from a
    # log call — it degrades gracefully instead.
    msgs, _ = _capture(lambda: log.error("only one %s and %s", "arg"))
    assert msgs  # something was emitted, no exception propagated


def test_callsite_attribution_points_at_caller_not_wrapper():
    def emit_here():
        log.warning("x=%s", 1)

    _, records = _capture(emit_here)
    assert records
    assert records[0]["function"] == "emit_here"
    assert records[0]["name"] == __name__
