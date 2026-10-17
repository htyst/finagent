"""Add masked instrument directory metadata; preserve existing account balances."""
from alembic import op
from sqlalchemy import Column, String, inspect

revision = '0018_directory'
down_revision = '0017_servicing'
branch_labels = None
depends_on = None


def upgrade():
    for table, columns, index in (
        ('cards', {'number_fingerprint': 64}, 'uq_card_number'),
        ('recipients', {'bank_name': 80, 'number_fingerprint': 64}, 'uq_recipient_number'),
    ):
        present = {column['name'] for column in inspect(op.get_bind()).get_columns(table)}
        for name, length in columns.items():
            if name not in present:
                op.add_column(table, Column(name, String(length), nullable=True))
        if index not in {row['name'] for row in inspect(op.get_bind()).get_indexes(table)}:
            op.create_index(index, table, ['owner_id', 'number_fingerprint'], unique=True)


def downgrade():
    for table, columns, index in (
        ('recipients', ['bank_name', 'number_fingerprint'], 'uq_recipient_number'),
        ('cards', ['number_fingerprint'], 'uq_card_number'),
    ):
        op.drop_index(index, table_name=table)
        # Supported Python/SQLite versions can drop these nullable columns
        # directly, preserving cards/recipients and their incoming foreign keys.
        for column in columns:
            op.drop_column(table, column)
