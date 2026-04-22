"""add_push_tokens

Revision ID: 202604211200
Revises: 202604180900
Create Date: 2026-04-21 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '202604211200'
down_revision = '202604180900'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'push_tokens',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('fcm_token', sa.String(), nullable=False),
        sa.Column('platform', sa.String(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_push_tokens_fcm_token'),
        'push_tokens',
        ['fcm_token'],
        unique=True,
    )
    op.create_index(
        op.f('ix_push_tokens_user_id'),
        'push_tokens',
        ['user_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_push_tokens_user_id'), table_name='push_tokens')
    op.drop_index(op.f('ix_push_tokens_fcm_token'), table_name='push_tokens')
    op.drop_table('push_tokens')
