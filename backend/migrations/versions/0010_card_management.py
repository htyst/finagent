"""Add card capabilities; existing tables and business rows remain untouched."""
from alembic import op
from app.data.models import Base
from app.data.card_models import CardControl
revision='0010_card_management'
down_revision='0009_wealth_workflows'
branch_labels=None
depends_on=None
TABLES=('card_controls','card_requests','card_payments')
def upgrade():
 for name in TABLES:Base.metadata.tables[name].create(op.get_bind(),checkfirst=True)
 op.get_bind().exec_driver_sql('INSERT INTO card_controls (card_id,limit_minor,online_enabled,lost,version) SELECT id,100000,1,0,1 FROM cards WHERE id NOT IN (SELECT card_id FROM card_controls)')
def downgrade():
 for name in reversed(TABLES):Base.metadata.tables[name].drop(op.get_bind(),checkfirst=True)
