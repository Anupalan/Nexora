"""request context columns

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('inference_requests', sa.Column('client_ip', sa.String(length=64), nullable=True))
    op.add_column('inference_requests', sa.Column('user_agent', sa.String(length=200), nullable=True))
    op.add_column('inference_requests', sa.Column('request_bytes', sa.Integer(), nullable=True))
    op.add_column('inference_requests', sa.Column('has_tools', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('inference_requests', sa.Column('finish_reason', sa.String(length=30), nullable=True))
    op.add_column('inference_requests', sa.Column('tool_calls', sa.Integer(), nullable=True))


def downgrade() -> None:
    for c in ('tool_calls', 'finish_reason', 'has_tools', 'request_bytes', 'user_agent', 'client_ip'):
        op.drop_column('inference_requests', c)
