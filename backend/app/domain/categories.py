"""One vocabulary for imported, newly posted and queried expense categories."""
from sqlalchemy import case,func
from app.domain.errors import DomainError

CATEGORIES={'餐饮','购物','交通','未分类','日用采购','文娱','医疗','居住','水电通信','教育','旅行','经营支出','保险保障','订阅续费','生日采购','礼物'}
ALIASES={'订阅服务':'订阅续费','餐饮消费':'餐饮','吃饭':'餐饮','娱乐':'文娱','通讯':'水电通信','保险':'保险保障'}

def canonical(value):
    value=(value or '').strip()
    return ALIASES.get(value,value or '未分类')

def category_expression(column):
    return case(*[(column==alias,name) for alias,name in ALIASES.items()],else_=func.coalesce(func.nullif(column,''),'未分类'))

def query_category(value):
    if not value or value in {'全部','全部类别'}:return None
    value=canonical(value)
    if value not in CATEGORIES:raise DomainError('INVALID_CATEGORY','无法识别消费类别，请选择已支持类别或查询全部类别',422)
    return value
