"""Add simulated products, assessments, orders and positions without changing existing data."""
from alembic import op
from app.data.models import Base
revision = '0009_wealth_workflows'
down_revision = '0008_banking_workflows'
branch_labels = None
depends_on = None
TABLES = ('wealth_products','risk_assessments','wealth_positions','wealth_orders')
def upgrade():
    for name in TABLES: Base.metadata.tables[name].create(op.get_bind(), checkfirst=True)
    # Global catalog contains only the three fictional data C products.
    from app.domain.wealth import PRODUCT_DATA
    connection=op.get_bind()
    table=Base.metadata.tables['wealth_products']
    for item in PRODUCT_DATA:
        if connection.execute(table.select().where(table.c.id==item['id'])).first() is None:
            connection.execute(table.insert().values(**item))
def downgrade():
    for name in reversed(TABLES): Base.metadata.tables[name].drop(op.get_bind(), checkfirst=True)
