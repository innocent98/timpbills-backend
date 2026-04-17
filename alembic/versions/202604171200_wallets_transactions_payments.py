"""wallets transactions payments idempotency webhooks

Revision ID: 202604171200
Revises: 202604151100
Create Date: 2026-04-17 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '202604171200'
down_revision = '202604151100'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create ENUM types explicitly (checkfirst=True makes this idempotent).
    # Must be done before create_table so the columns reference pre-existing types
    # (create_type=False below tells SQLAlchemy not to re-emit CREATE TYPE).
    tx_status_enum = postgresql.ENUM(
        'pending', 'processing', 'success', 'failed',
        'refund_pending', 'refunded', 'refund_failed',
        name='tx_status_enum', create_type=True,
    )
    tx_status_enum.create(op.get_bind(), checkfirst=True)

    tx_type_enum = postgresql.ENUM(
        'wallet_funding', 'airtime', 'data', 'electricity', 'cable', 'flight', 'refund',
        name='tx_type_enum', create_type=True,
    )
    tx_type_enum.create(op.get_bind(), checkfirst=True)

    payment_status_enum = postgresql.ENUM(
        'pending', 'success', 'failed',
        name='payment_status_enum', create_type=True,
    )
    payment_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table('idempotency_keys',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('request_hash', sa.String(), nullable=False),
    sa.Column('response_status', sa.Integer(), nullable=False),
    sa.Column('response_body', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_idempotency_keys_key'), 'idempotency_keys', ['key'], unique=True)
    op.create_index(op.f('ix_idempotency_keys_user_id'), 'idempotency_keys', ['user_id'], unique=False)

    op.create_table('webhook_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('provider', sa.String(), nullable=False),
    sa.Column('provider_event_id', sa.String(), nullable=False),
    sa.Column('event_type', sa.String(), nullable=False),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('processed', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_webhook_events_provider_event_id'), 'webhook_events', ['provider_event_id'], unique=True)

    op.create_table('transactions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('reference', sa.String(), nullable=False),
    sa.Column('type', postgresql.ENUM(
        'wallet_funding', 'airtime', 'data', 'electricity', 'cable', 'flight', 'refund',
        name='tx_type_enum', create_type=False,
    ), nullable=False),
    sa.Column('status', postgresql.ENUM(
        'pending', 'processing', 'success', 'failed',
        'refund_pending', 'refunded', 'refund_failed',
        name='tx_status_enum', create_type=False,
    ), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('fee', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('reference')
    )
    op.create_index('ix_tx_reference', 'transactions', ['reference'], unique=True)
    op.create_index('ix_tx_user_created', 'transactions', ['user_id', 'created_at'], unique=False)

    op.create_table('wallets',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('balance', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('balance_cap', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('balance >= 0', name='wallet_balance_non_negative'),
    sa.CheckConstraint('balance_cap >= 0', name='wallet_cap_non_negative'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_wallets_user_id'), 'wallets', ['user_id'], unique=True)

    op.create_table('payments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('transaction_id', sa.UUID(), nullable=False),
    sa.Column('provider', sa.String(), nullable=False),
    sa.Column('provider_reference', sa.String(), nullable=False),
    sa.Column('status', postgresql.ENUM(
        'pending', 'success', 'failed',
        name='payment_status_enum', create_type=False,
    ), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_payments_provider_reference'), 'payments', ['provider_reference'], unique=True)
    op.create_index(op.f('ix_payments_transaction_id'), 'payments', ['transaction_id'], unique=True)

    op.create_table('transaction_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('transaction_id', sa.UUID(), nullable=False),
    sa.Column('from_status', postgresql.ENUM(
        'pending', 'processing', 'success', 'failed',
        'refund_pending', 'refunded', 'refund_failed',
        name='tx_status_enum', create_type=False,
    ), nullable=True),
    sa.Column('to_status', postgresql.ENUM(
        'pending', 'processing', 'success', 'failed',
        'refund_pending', 'refunded', 'refund_failed',
        name='tx_status_enum', create_type=False,
    ), nullable=False),
    sa.Column('reason', sa.String(), nullable=True),
    sa.Column('context', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_transaction_events_transaction_id'), 'transaction_events', ['transaction_id'], unique=False)

    op.add_column('otp_codes', sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.alter_column('otp_codes', 'expires_at',
               existing_type=postgresql.TIMESTAMP(),
               type_=sa.DateTime(timezone=True),
               existing_nullable=False)
    op.alter_column('otp_codes', 'used_at',
               existing_type=postgresql.TIMESTAMP(),
               type_=sa.DateTime(timezone=True),
               existing_nullable=True)
    op.alter_column('otp_codes', 'created_at',
               existing_type=postgresql.TIMESTAMP(),
               type_=sa.DateTime(timezone=True),
               existing_nullable=False,
               existing_server_default=sa.text('now()'))
    op.alter_column('users', 'created_at',
               existing_type=postgresql.TIMESTAMP(),
               type_=sa.DateTime(timezone=True),
               existing_nullable=False,
               existing_server_default=sa.text('now()'))
    op.alter_column('users', 'updated_at',
               existing_type=postgresql.TIMESTAMP(),
               type_=sa.DateTime(timezone=True),
               existing_nullable=False,
               existing_server_default=sa.text('now()'))


def downgrade() -> None:
    op.alter_column('users', 'updated_at',
               existing_type=sa.DateTime(timezone=True),
               type_=postgresql.TIMESTAMP(),
               existing_nullable=False,
               existing_server_default=sa.text('now()'))
    op.alter_column('users', 'created_at',
               existing_type=sa.DateTime(timezone=True),
               type_=postgresql.TIMESTAMP(),
               existing_nullable=False,
               existing_server_default=sa.text('now()'))
    op.alter_column('otp_codes', 'created_at',
               existing_type=sa.DateTime(timezone=True),
               type_=postgresql.TIMESTAMP(),
               existing_nullable=False,
               existing_server_default=sa.text('now()'))
    op.alter_column('otp_codes', 'used_at',
               existing_type=sa.DateTime(timezone=True),
               type_=postgresql.TIMESTAMP(),
               existing_nullable=True)
    op.alter_column('otp_codes', 'expires_at',
               existing_type=sa.DateTime(timezone=True),
               type_=postgresql.TIMESTAMP(),
               existing_nullable=False)
    op.drop_column('otp_codes', 'updated_at')

    op.drop_index(op.f('ix_transaction_events_transaction_id'), table_name='transaction_events')
    op.drop_table('transaction_events')
    op.drop_index(op.f('ix_payments_transaction_id'), table_name='payments')
    op.drop_index(op.f('ix_payments_provider_reference'), table_name='payments')
    op.drop_table('payments')
    op.drop_index(op.f('ix_wallets_user_id'), table_name='wallets')
    op.drop_table('wallets')
    op.drop_index('ix_tx_user_created', table_name='transactions')
    op.drop_index('ix_tx_reference', table_name='transactions')
    op.drop_table('transactions')
    op.drop_index(op.f('ix_webhook_events_provider_event_id'), table_name='webhook_events')
    op.drop_table('webhook_events')
    op.drop_index(op.f('ix_idempotency_keys_user_id'), table_name='idempotency_keys')
    op.drop_index(op.f('ix_idempotency_keys_key'), table_name='idempotency_keys')
    op.drop_table('idempotency_keys')

    op.execute("DROP TYPE IF EXISTS payment_status_enum")
    op.execute("DROP TYPE IF EXISTS tx_type_enum")
    op.execute("DROP TYPE IF EXISTS tx_status_enum")
