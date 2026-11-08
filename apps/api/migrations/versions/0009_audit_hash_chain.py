"""audit hash chain

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('audit_logs', sa.Column('seq', sa.BigInteger(), nullable=True))
    op.add_column('audit_logs', sa.Column('prev_hash', sa.String(length=64), nullable=True))
    op.add_column('audit_logs', sa.Column('hash', sa.String(length=64), nullable=True))
    op.create_unique_constraint('uq_audit_logs_seq', 'audit_logs', ['seq'])
    # Still append-only, with one exception: a row that has not been sealed yet may receive its chain
    # columns (seq, prev_hash, hash) and nothing else. Once sealed, a row can never change again.
    op.execute("""
        CREATE OR REPLACE FUNCTION audit_logs_immutable() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'UPDATE' AND OLD.hash IS NULL
             AND NEW.id = OLD.id AND NEW.actor_id IS NOT DISTINCT FROM OLD.actor_id
             AND NEW.action = OLD.action AND NEW.entity_type = OLD.entity_type
             AND NEW.entity_id IS NOT DISTINCT FROM OLD.entity_id
             AND NEW.metadata::text = OLD.metadata::text AND NEW.created_at = OLD.created_at THEN
            RETURN NEW;
          END IF;
          RAISE EXCEPTION 'audit_logs is append-only';
        END;
        $$ LANGUAGE plpgsql
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION audit_logs_immutable() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'audit_logs is append-only'; END;
        $$ LANGUAGE plpgsql
    """)
    op.drop_constraint('uq_audit_logs_seq', 'audit_logs', type_='unique')
    op.drop_column('audit_logs', 'hash')
    op.drop_column('audit_logs', 'prev_hash')
    op.drop_column('audit_logs', 'seq')
