import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.admin_user import AdminRole, AdminUser


def test_admin_user_defaults(db_session):
    admin = AdminUser(
        email="ops@timpbills.com",
        password_hash="x",
        full_name="Ops One",
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    assert admin.id is not None
    assert admin.role is AdminRole.superadmin   # default
    assert admin.is_active is True
    assert admin.last_login_at is None


def test_admin_email_is_unique(db_session):
    db_session.add(AdminUser(email="dup@x.com", password_hash="a", full_name="A"))
    db_session.commit()
    db_session.add(AdminUser(email="dup@x.com", password_hash="b", full_name="B"))
    with pytest.raises(IntegrityError):
        db_session.commit()
