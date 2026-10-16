"""M4 persisted single-scenario conversations.

Revision ID: 0003_m4_agent
Revises: 0002_m3_operations
"""
from alembic import op
from app.data.models import AgentMessage, AgentStep, AgentTask, Base

revision = "0003_m4_agent"
down_revision = "0002_m3_operations"
branch_labels = None
depends_on = None
TABLES = (AgentTask.__table__, AgentMessage.__table__, AgentStep.__table__)

def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=TABLES, checkfirst=True)

def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=list(reversed(TABLES)), checkfirst=True)
