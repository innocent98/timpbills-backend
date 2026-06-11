from app.db.models.admin_user import AdminUser
from scripts.create_admin import create_admin


def test_create_admin_inserts(db_session):
    admin = create_admin(
        db_session, email="boss@x.com", password="longpassword", full_name="Boss"
    )
    assert admin.id is not None
    assert db_session.query(AdminUser).filter_by(email="boss@x.com").count() == 1


def test_create_admin_idempotent(db_session):
    create_admin(db_session, email="boss@x.com", password="longpassword", full_name="Boss")
    again = create_admin(db_session, email="boss@x.com", password="other", full_name="Boss2")
    # returns the existing row, does not duplicate
    assert db_session.query(AdminUser).filter_by(email="boss@x.com").count() == 1
    assert again.full_name == "Boss"  # unchanged
