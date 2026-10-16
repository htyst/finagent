"""M5 bounded plans and confirmation condition evidence."""
from alembic import op
from sqlalchemy import Column, Integer, String, inspect
revision = "0004_m5_plans"
down_revision = "0003_m4_agent"
branch_labels = None
depends_on = None

def upgrade():
    additions = {
        "proposals": [Column("validation_json", String(), nullable=True)],
        "agent_tasks": [Column("plan_version", Integer(), nullable=False, server_default="1"),
                        Column("plan_json", String(), nullable=False, server_default="[]"),
                        Column("requested_outcomes_json", String(), nullable=False, server_default="[]"),
                        Column("stop_requested", Integer(), nullable=False, server_default="0")],
        "agent_steps": [Column("plan_version", Integer(), nullable=False, server_default="1"),
                        Column("depends_on_json", String(), nullable=False, server_default="[]"),
                        Column("on_failure", String(), nullable=False, server_default="stop"),
                        Column("result_ref", String(), nullable=True)],
    }
    for table, columns in additions.items():
        existing = {col["name"] for col in inspect(op.get_bind()).get_columns(table)}
        for col in columns:
            if col.name not in existing:
                op.add_column(table, col)

def downgrade():
    for table, names in {"agent_steps": ["result_ref", "on_failure", "depends_on_json", "plan_version"],
                         "agent_tasks": ["stop_requested", "requested_outcomes_json", "plan_json", "plan_version"],
                         "proposals": ["validation_json"]}.items():
        for name in names:
            op.drop_column(table, name)
