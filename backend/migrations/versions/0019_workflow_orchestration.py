"""Durable follow-ups and explicitly consented read-only reviews."""
from alembic import op
from app.data.workflow_models import WorkflowJob, WorkflowReview, WorkflowReviewRun

revision = '0019_workflow_orchestration'
down_revision = '0018_directory'
branch_labels = None
depends_on = None


def upgrade():
    for model in (WorkflowJob, WorkflowReview, WorkflowReviewRun):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    for model in (WorkflowReviewRun, WorkflowReview, WorkflowJob):
        model.__table__.drop(op.get_bind(), checkfirst=True)
