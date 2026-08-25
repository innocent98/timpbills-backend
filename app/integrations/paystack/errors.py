"""Provider-level exceptions shared by the real Paystack client and the fake.

Kept out of client.py so that callers which only need to *catch* a provider
rejection -- API endpoints, the in-memory fake -- do not have to import the
httpx- and tenacity-backed client module to do it.
"""


class PaystackError(Exception):
    """Paystack answered the call but rejected it (`status: false`).

    Distinct from a transport failure: the request reached Paystack and was
    refused, so the message is Paystack's own and is safe to log, though not
    necessarily safe to show a user verbatim.
    """
