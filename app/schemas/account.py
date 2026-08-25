from datetime import datetime

from pydantic import BaseModel, Field


class AccountDeletionRequest(BaseModel):
    identifier: str = Field(..., description="Registered email or phone number")
    password: str


class AccountDeletionResponse(BaseModel):
    scheduled_deletion_at: datetime


class CancelDeletionResponse(BaseModel):
    cancelled: bool
