"""Shared SQL enum types — one place so Alembic picks them up consistently."""
import enum


class TransactionStatus(str, enum.Enum):
    pending        = "pending"
    processing     = "processing"
    success        = "success"
    failed         = "failed"
    refund_pending = "refund_pending"
    refunded       = "refunded"
    refund_failed  = "refund_failed"


class TransactionType(str, enum.Enum):
    wallet_funding = "wallet_funding"
    airtime        = "airtime"
    data           = "data"
    electricity    = "electricity"
    cable          = "cable"
    flight         = "flight"
    refund         = "refund"
