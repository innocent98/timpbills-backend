# This file has been superseded by:
#   tests/services/test_auth_service_email_verification.py
#   tests/services/test_auth_service_phone_verification.py
#
# The original phone-based register OTP flow was replaced in E2 with an
# email-first verification gate. The legacy verify_otp() method is kept on
# AuthService only for internal backward compatibility during the migration
# window and is tested indirectly via the service-layer tests.
