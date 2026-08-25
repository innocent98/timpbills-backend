import uuid
from app.db.models.user import User


def test_user_has_nullable_anonymized_at(db_session):
    u = User(
        id=uuid.uuid4(), email="a@b.co", phone="+2348100000001",
        full_name="A B", password_hash="x", is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    assert u.anonymized_at is None
    # column is settable
    from datetime import UTC, datetime
    u.anonymized_at = datetime.now(UTC)
    db_session.commit()
    assert u.anonymized_at is not None
