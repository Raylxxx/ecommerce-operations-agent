from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, func
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import config

engine = create_engine(config.DATABASE_URL, pool_pre_ping=True, pool_recycle=3600) if config.DATABASE_URL else None
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="viewer")  # admin / operator / viewer
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))


class OpLog(Base):
    __tablename__ = "op_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50))
    action: Mapped[str] = mapped_column(String(50), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(50), index=True)
    cost: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    stock: Mapped[int] = mapped_column(Integer, default=0)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[float] = mapped_column(Float)
    segment: Mapped[str] = mapped_column(String(50), default="未知")
    region: Mapped[str] = mapped_column(String(50), default="未知")
    order_date: Mapped[date] = mapped_column(Date, index=True)


class CompetitorPrice(Base):
    __tablename__ = "competitor_prices"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    competitor: Mapped[str] = mapped_column(String(100))
    price: Mapped[float] = mapped_column(Float)
    captured_on: Mapped[date] = mapped_column(Date, index=True)


class MarketTrend(Base):
    __tablename__ = "market_trends"
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(50), index=True)
    month: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    search_index: Mapped[int] = mapped_column(Integer)
    avg_price: Mapped[float] = mapped_column(Float)


class Candidate(Base):
    """待评估的选品候选。"""
    __tablename__ = "candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(50), index=True)
    est_cost: Mapped[float] = mapped_column(Float)
    est_price: Mapped[float] = mapped_column(Float)
    monthly_demand: Mapped[int] = mapped_column(Integer)
    growth: Mapped[float] = mapped_column(Float)  # 需求环比增长
    competitor_count: Mapped[int] = mapped_column(Integer)


class PriceProposal(Base):
    """Agent 只能提交调价建议，人工批准后才会改价。"""
    __tablename__ = "price_proposals"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    old_price: Mapped[float] = mapped_column(Float)
    new_price: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending / approved / rejected
    reviewed_by: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    goal: Mapped[str] = mapped_column(String(255), default="")
    budget: Mapped[float] = mapped_column(Float, default=0)
    plan: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ABTest(Base):
    __tablename__ = "ab_tests"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    variant_a: Mapped[str] = mapped_column(Text)
    variant_b: Mapped[str] = mapped_column(Text)
    a_exposures: Mapped[int] = mapped_column(Integer, default=0)
    a_conversions: Mapped[int] = mapped_column(Integer, default=0)
    b_exposures: Mapped[int] = mapped_column(Integer, default=0)
    b_conversions: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class KBDocument(Base):
    __tablename__ = "kb_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class KBChunk(Base):
    __tablename__ = "kb_chunks"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("kb_documents.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT(), "mysql"))


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(20), index=True)
    username: Mapped[str] = mapped_column(String(50))
    task: Mapped[str] = mapped_column(Text)
    output: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT(), "mysql"), default="")
    steps: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)  # running / success / failed
    error: Mapped[str] = mapped_column(Text, default="")
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


def get_setting(db, key: str) -> str:
    row = db.get(Setting, key)
    return row.value if row else config.DEFAULT_SETTINGS[key]
