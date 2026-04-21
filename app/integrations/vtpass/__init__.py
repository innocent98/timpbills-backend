"""VTPass integration — unified aggregator for airtime, data, electricity
and cable TV bill payments. Mirrors the Paystack adapter pattern: a
Protocol in `base.py`, typed Pydantic models in `schemas.py`, a fake in
`fake.py`, a real HTTPX client in `client.py`, and an env-gated factory
in `factory.py`."""
