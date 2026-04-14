import re
from pydantic import BaseModel, EmailStr, Field, field_validator

_NIGERIAN_PHONE_RE = re.compile(r"^(\+234|0)[789][01]\d{8}$")


class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=80)
    phone: str
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        if not _NIGERIAN_PHONE_RE.match(v):
            raise ValueError("Invalid Nigerian phone number")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain an uppercase letter")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain a lowercase letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain a digit")
        return v


class RegisterResponse(BaseModel):
    user_id: str
    phone: str


class VerifyOtpRequest(BaseModel):
    phone: str
    code: str = Field(min_length=6, max_length=6)


class AuthTokens(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int


class VerifyOtpResponse(BaseModel):
    tokens: AuthTokens
    pin_set: bool


class LoginRequest(BaseModel):
    identifier: str
    password: str


class LoginResponse(BaseModel):
    tokens: AuthTokens
    pin_set: bool


class RefreshRequest(BaseModel):
    refresh_token: str


class SetPinRequest(BaseModel):
    pin: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")


class ForgotPasswordRequest(BaseModel):
    identifier: str


class ResetPasswordRequest(BaseModel):
    identifier: str
    code: str = Field(min_length=6, max_length=6)
    new_password: str
