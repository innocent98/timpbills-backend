"""Sprint 5c task 1.2 — ORM coverage of the new profile columns.

Migration ``202605201200`` (Sprint 5c task 1.1) added four nullable profile
columns to ``users`` and created the ``notification_preferences`` table.
These tests pin the ORM side: the SQLAlchemy ``User`` model must expose the
new attributes (default ``None``) and accept assigned values without
construction-time errors. No DB session needed — pure in-memory model
instantiation.
"""
import datetime as dt

from app.db.models.user import User


def test_user_has_sprint_5c_attributes():
    u = User(
        email="t@example.com",
        phone="+2348011111111",
        password_hash="x",
        full_name="Test User",
    )
    # These attributes must exist (default None) without raising
    assert u.date_of_birth is None
    assert u.gender is None
    assert u.address is None
    assert u.avatar_url is None


def test_user_accepts_sprint_5c_values():
    u = User(
        email="t@example.com",
        phone="+2348011111111",
        password_hash="x",
        full_name="Test User",
        date_of_birth=dt.date(1990, 1, 15),
        gender="male",
        address="123 Main St, Lagos",
        avatar_url="https://res.cloudinary.com/x/avatar/u123.jpg",
    )
    assert u.date_of_birth == dt.date(1990, 1, 15)
    assert u.gender == "male"
    assert u.address == "123 Main St, Lagos"
    assert u.avatar_url.startswith("https://")
