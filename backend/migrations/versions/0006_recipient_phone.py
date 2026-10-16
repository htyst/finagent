"""Add nullable phone identifiers without inferring phones for existing recipients."""
from alembic import op
import sqlalchemy as sa
revision = "0006_recipient_phone"
down_revision = "0005_recipient_credits"
branch_labels = None
depends_on = None

def upgrade():
    if "phone" not in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("recipients")}:
        op.add_column("recipients", sa.Column("phone", sa.String(11), nullable=True))

def downgrade():
    op.drop_column("recipients", "phone")
