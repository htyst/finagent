"""Add durable security state without changing existing financial data."""
from alembic import op
from app.data.models import Base
from app.data import security_models  # noqa: F401

revision = '0013_security'
down_revision = '0012_birthday_workflows'
branch_labels = None
depends_on = None
TABLES = ('security_states', 'security_challenges', 'security_authorizations', 'task_controls')


def upgrade():
    for name in TABLES:
        Base.metadata.tables[name].create(op.get_bind(), checkfirst=True)


def downgrade():
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(op.get_bind(), checkfirst=True)
