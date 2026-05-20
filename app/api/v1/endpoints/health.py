from datetime import UTC, datetime

from fastapi import APIRouter

router = APIRouter()


@router.get("")
def health_check():
    return {
        "status": "ok",
        "timestamp": datetime.now(UTC).isoformat()
    }
