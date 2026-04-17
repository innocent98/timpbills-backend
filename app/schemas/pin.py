from pydantic import BaseModel, Field


class VerifyPinRequest(BaseModel):
    pin: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")


class VerifyPinResponse(BaseModel):
    pin_token: str
    expires_in: int
