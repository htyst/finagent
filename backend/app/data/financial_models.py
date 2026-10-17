"""Lossless imported financial snapshots, separate from execution authority."""
from sqlalchemy import ForeignKey,Index,Integer,String
from sqlalchemy.orm import Mapped,mapped_column
from app.data.models import Base


class FinancialDatasetImport(Base):
    __tablename__='financial_dataset_imports'
    dataset_version:Mapped[str]=mapped_column(String,primary_key=True)
    manifest_json:Mapped[str]=mapped_column(String,nullable=False)
    imported_at_utc:Mapped[str]=mapped_column(String,nullable=False)


class FinancialDatasetRow(Base):
    __tablename__='financial_dataset_rows'
    __table_args__=(Index('ix_financial_owner_table','owner_id','table_name','dataset_version'),)
    dataset_version:Mapped[str]=mapped_column(String,primary_key=True)
    table_name:Mapped[str]=mapped_column(String,primary_key=True)
    row_key:Mapped[str]=mapped_column(String,primary_key=True)
    owner_id:Mapped[str|None]=mapped_column(ForeignKey('users.id'),nullable=True)
    payload_json:Mapped[str]=mapped_column(String,nullable=False)


class FinancialEvent(Base):
    """Indexed read projection of imported debt events; never a cash ledger."""
    __tablename__='financial_events'
    __table_args__=(Index('ix_financial_event_owner_date','owner_id','dataset_version','posted_at_utc'),
                   Index('ix_financial_event_card_date','card_id','posted_at_utc'))
    dataset_version:Mapped[str]=mapped_column(String,primary_key=True)
    id:Mapped[str]=mapped_column(String,primary_key=True)
    owner_id:Mapped[str]=mapped_column(ForeignKey('users.id'),nullable=False)
    card_id:Mapped[str|None]=mapped_column(String)
    liability_id:Mapped[str]=mapped_column(String,nullable=False)
    event_type:Mapped[str]=mapped_column(String,nullable=False)
    amount_minor:Mapped[int]=mapped_column(Integer,nullable=False)
    debt_delta_minor:Mapped[int]=mapped_column(Integer,nullable=False)
    category:Mapped[str|None]=mapped_column(String)
    merchant:Mapped[str|None]=mapped_column(String)
    posted_at_utc:Mapped[str]=mapped_column(String,nullable=False)
    subscription_id:Mapped[str|None]=mapped_column(String)
    cash_transaction_id:Mapped[str|None]=mapped_column(String)
    business_id:Mapped[str|None]=mapped_column(String)
