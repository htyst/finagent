"""Durable simulator servicing requests; imported financial rows remain immutable."""
from sqlalchemy import CheckConstraint,ForeignKey,Integer,String
from sqlalchemy.orm import Mapped,mapped_column
from app.data.models import Base


class ServiceRequest(Base):
    __tablename__='service_requests'
    __table_args__=(CheckConstraint("kind IN ('replace_card','credit_limit','cancel_contract')"),
        CheckConstraint("status IN ('pending_confirmation','submitted','executed','rejected','cancelled','invalidated','expired')"),)
    id:Mapped[str]=mapped_column(String,primary_key=True)
    owner_id:Mapped[str]=mapped_column(ForeignKey('users.id'),index=True)
    session_id:Mapped[str]=mapped_column(String)
    dataset_version:Mapped[str]=mapped_column(String)
    agent_task_id:Mapped[str|None]=mapped_column(ForeignKey('agent_tasks.id'),nullable=True)
    kind:Mapped[str]=mapped_column(String)
    subject_id:Mapped[str]=mapped_column(String)
    status:Mapped[str]=mapped_column(String)
    terms_json:Mapped[str]=mapped_column(String)
    terms_digest:Mapped[str]=mapped_column(String)
    token_hash:Mapped[str|None]=mapped_column(String,nullable=True)
    receipt_json:Mapped[str|None]=mapped_column(String,nullable=True)
    created_at_utc:Mapped[str]=mapped_column(String)
    expires_at_utc:Mapped[str]=mapped_column(String)
    completed_at_utc:Mapped[str|None]=mapped_column(String,nullable=True)


class CreditFacility(Base):
    __tablename__='credit_facilities'
    __table_args__=(CheckConstraint('limit_minor > 0'),)
    dataset_version:Mapped[str]=mapped_column(String,primary_key=True)
    card_id:Mapped[str]=mapped_column(String,primary_key=True)
    owner_id:Mapped[str]=mapped_column(ForeignKey('users.id'),index=True)
    source_limit_minor:Mapped[int]=mapped_column(Integer)
    limit_minor:Mapped[int]=mapped_column(Integer)
    version:Mapped[int]=mapped_column(Integer)
    updated_at_utc:Mapped[str]=mapped_column(String)


class MerchantContractState(Base):
    __tablename__='merchant_contract_states'
    dataset_version:Mapped[str]=mapped_column(String,primary_key=True)
    contract_id:Mapped[str]=mapped_column(String,primary_key=True)
    owner_id:Mapped[str]=mapped_column(ForeignKey('users.id'),index=True)
    access_end_date:Mapped[str|None]=mapped_column(String,nullable=True)
    cancelled_at_utc:Mapped[str]=mapped_column(String)
    request_id:Mapped[str]=mapped_column(ForeignKey('service_requests.id'),unique=True)
