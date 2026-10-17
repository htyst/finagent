"""Card controls, owner-scoped requests and idempotent payment records."""
from sqlalchemy import CheckConstraint,ForeignKey,Integer,String,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from app.data.models import Base
class CardControl(Base):
 __tablename__='card_controls'
 __table_args__=(CheckConstraint('limit_minor > 0'),CheckConstraint('online_enabled IN (0,1)'),CheckConstraint('lost IN (0,1)'),)
 card_id:Mapped[str]=mapped_column(ForeignKey('cards.id'),primary_key=True)
 limit_minor:Mapped[int]=mapped_column(Integer,nullable=False,default=100000)
 online_enabled:Mapped[int]=mapped_column(Integer,nullable=False,default=1)
 lost:Mapped[int]=mapped_column(Integer,nullable=False,default=0)
 version:Mapped[int]=mapped_column(Integer,nullable=False,default=1)
class CardRequest(Base):
 __tablename__='card_requests'
 __table_args__=(CheckConstraint("kind IN ('apply','limit','online_block','online_unblock','loss','unloss')"),CheckConstraint("status IN ('pending_confirmation','submitted','issued','executed','cancelled','invalidated','expired')"),)
 id:Mapped[str]=mapped_column(String,primary_key=True)
 owner_id:Mapped[str]=mapped_column(ForeignKey('users.id'),nullable=False,index=True)
 session_id:Mapped[str]=mapped_column(String,nullable=False)
 dataset_version:Mapped[str]=mapped_column(String,nullable=False)
 agent_task_id:Mapped[str|None]=mapped_column(ForeignKey('agent_tasks.id'))
 kind:Mapped[str]=mapped_column(String,nullable=False)
 card_id:Mapped[str|None]=mapped_column(ForeignKey('cards.id'))
 account_id:Mapped[str]=mapped_column(ForeignKey('accounts.id'),nullable=False)
 status:Mapped[str]=mapped_column(String,nullable=False)
 terms_json:Mapped[str]=mapped_column(String,nullable=False)
 terms_digest:Mapped[str]=mapped_column(String,nullable=False)
 token_hash:Mapped[str|None]=mapped_column(String)
 receipt_json:Mapped[str|None]=mapped_column(String)
 issued_card_id:Mapped[str|None]=mapped_column(ForeignKey('cards.id'))
 created_at_utc:Mapped[str]=mapped_column(String,nullable=False)
 expires_at_utc:Mapped[str]=mapped_column(String,nullable=False)
 submitted_at_utc:Mapped[str|None]=mapped_column(String)
 completed_at_utc:Mapped[str|None]=mapped_column(String)
class CardPayment(Base):
 __tablename__='card_payments'
 __table_args__=(UniqueConstraint('owner_id','idempotency_key'),CheckConstraint('amount_minor > 0'),CheckConstraint("channel IN ('online','offline')"),CheckConstraint("status IN ('posted','rejected')"),)
 id:Mapped[str]=mapped_column(String,primary_key=True)
 owner_id:Mapped[str]=mapped_column(ForeignKey('users.id'),nullable=False)
 card_id:Mapped[str]=mapped_column(ForeignKey('cards.id'),nullable=False)
 idempotency_key:Mapped[str]=mapped_column(String,nullable=False)
 amount_minor:Mapped[int]=mapped_column(Integer,nullable=False)
 channel:Mapped[str]=mapped_column(String,nullable=False)
 status:Mapped[str]=mapped_column(String,nullable=False)
 error_code:Mapped[str|None]=mapped_column(String)
 transaction_id:Mapped[str|None]=mapped_column(ForeignKey('transactions.id'),unique=True)
 balance_minor:Mapped[int]=mapped_column(Integer,nullable=False)
 created_at_utc:Mapped[str]=mapped_column(String,nullable=False)
