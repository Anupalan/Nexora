"""node memory budget (for the warm-up manager)

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('inference_nodes', sa.Column('memory_budget_gb', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('inference_nodes', 'memory_budget_gb')
