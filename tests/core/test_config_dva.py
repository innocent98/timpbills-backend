from app.core.config import settings


def test_dva_defaults_present():
    assert settings.PAYSTACK_DVA_PREFERRED_BANK == "wema-bank"
    # Accounting/reporting only — never applied to the wallet credit.
    assert isinstance(settings.PAYSTACK_DVA_FEE_PERCENT, float)
    assert isinstance(settings.PAYSTACK_DVA_FEE_CAP_NGN, int)
