import csv
import hashlib
import hmac
import io
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import jwt
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select

from . import agents, config, kb
from .models import (ABTest, AgentRun, Base, Campaign, Candidate, CompetitorPrice, KBDocument, MarketTrend, OpLog,
                     Order, PriceProposal, Product, SessionLocal, Setting, User, engine, get_setting)
from .tools import ab_stats

ROLE_LEVEL = {"viewer": 0, "operator": 1, "admin": 2}
INDEX_HTML = Path(__file__).resolve().parent.parent / "static" / "index.html"
bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt, digest = stored.split("$")
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(actual.hex(), digest)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(cred: HTTPAuthorizationCredentials | None = Depends(bearer), db=Depends(get_db)) -> User:
    if not cred:
        raise HTTPException(401, "请先登录")
    try:
        payload = jwt.decode(cred.credentials, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "登录已失效，请重新登录")
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(401, "账号不可用")
    return user


def require(role: str):
    def check(user: User = Depends(current_user)) -> User:
        if ROLE_LEVEL[user.role] < ROLE_LEVEL[role]:
            raise HTTPException(403, "没有权限执行此操作")
        return user
    return check


def log(db, user: User, action: str, detail: str = "") -> None:
    db.add(OpLog(username=user.username, action=action, detail=detail[:2000]))
    db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not config.DATABASE_URL or not config.JWT_SECRET or not config.ADMIN_PASSWORD:
        raise RuntimeError("缺少配置：请在 .env 中设置 DATABASE_URL、JWT_SECRET、ADMIN_PASSWORD")
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        for key, value in config.DEFAULT_SETTINGS.items():
            if not db.get(Setting, key):
                db.add(Setting(key=key, value=value))
        if not db.scalar(select(User.id).limit(1)):
            db.add(User(username="admin", password_hash=hash_password(config.ADMIN_PASSWORD), role="admin"))
        db.commit()
    yield


app = FastAPI(title="电商运营 Agent", lifespan=lifespan)


# ---------- 登录 ----------
class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginIn, db=Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username))
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=config.JWT_HOURS)
    token = jwt.encode({"sub": str(user.id), "exp": exp}, config.JWT_SECRET, algorithm="HS256")
    log(db, user, "login")
    return {"token": token, "username": user.username, "role": user.role}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {"username": user.username, "role": user.role}


# ---------- 数据看板 ----------
@app.get("/api/dashboard")
def dashboard(db=Depends(get_db), _: User = Depends(current_user)):
    end = db.scalar(select(func.max(Order.order_date))) or date.today()
    trend_start = end - timedelta(days=89)
    amount = Order.quantity * Order.unit_price
    profit = Order.quantity * (Order.unit_price - Product.cost)
    in_window = Order.order_date > end - timedelta(days=30)
    in_prior = Order.order_date.between(end - timedelta(days=59), end - timedelta(days=30))

    def total(expr, cond):
        return float(db.scalar(select(func.sum(case((cond, expr), else_=0))).join(Product, Order.product_id == Product.id)) or 0)

    revenue, prior = total(amount, in_window), total(amount, in_prior)
    trend = db.execute(
        select(Order.order_date, func.sum(amount).label("revenue"))
        .where(Order.order_date >= trend_start).group_by(Order.order_date).order_by(Order.order_date)
    ).all()
    categories = db.execute(
        select(Product.category, func.sum(amount).label("revenue"), func.sum(profit).label("profit"))
        .join(Product, Order.product_id == Product.id).where(in_window)
        .group_by(Product.category).order_by(func.sum(amount).desc())
    ).all()
    agent_status = []
    for key, agent in agents.AGENTS.items():
        counts = db.execute(
            select(func.count(), func.sum(case((AgentRun.status == "success", 1), else_=0)),
                   func.avg(AgentRun.latency_ms), func.max(AgentRun.created_at))
            .where(AgentRun.agent == key)
        ).one()
        agent_status.append({"key": key, "name": agent["name"], "runs": counts[0], "success": int(counts[1] or 0),
                             "avg_latency_ms": int(counts[2] or 0), "last_run": counts[3]})
    return {
        "as_of": end,
        "demo_data": bool(db.get(Setting, "demo_data")),
        "kpi": {
            "revenue_30d": round(revenue, 2),
            "revenue_change": round(revenue / prior - 1, 4) if prior else None,
            "orders_30d": db.scalar(select(func.count()).select_from(Order).where(in_window)) or 0,
            "margin_30d": round(total(profit, in_window) / revenue, 4) if revenue else 0,
            "pending_proposals": db.scalar(select(func.count()).select_from(PriceProposal).where(PriceProposal.status == "pending")) or 0,
        },
        "trend": [{"date": r.order_date, "revenue": round(float(r.revenue), 2)} for r in trend],
        "categories": [{"category": r.category, "revenue": round(float(r.revenue), 2),
                        "margin": round(float(r.profit) / float(r.revenue), 4) if r.revenue else 0} for r in categories],
        "agents": agent_status,
    }


@app.get("/api/products")
def products(db=Depends(get_db), _: User = Depends(current_user)):
    return [{"sku": p.sku, "name": p.name, "category": p.category, "cost": p.cost, "price": p.price, "stock": p.stock}
            for p in db.scalars(select(Product).order_by(Product.category, Product.sku))]


# ---------- Agent ----------
class TaskIn(BaseModel):
    task: str = Field(min_length=2, max_length=4000)


def run_view(run: AgentRun) -> dict:
    return {"id": run.id, "agent": run.agent, "agent_name": agents.AGENTS[run.agent]["name"], "username": run.username,
            "task": run.task, "output": run.output, "steps": run.steps, "status": run.status, "error": run.error,
            "tokens": run.tokens, "latency_ms": run.latency_ms, "created_at": run.created_at}


@app.post("/api/agents/{key}/run")
def run_agent(key: str, body: TaskIn, db=Depends(get_db), user: User = Depends(require("operator"))):
    if key == "auto":
        try:
            key = agents.route(db, body.task)
        except Exception as exc:
            raise HTTPException(502, f"任务分配失败：{exc}")
    if key not in agents.AGENTS:
        raise HTTPException(404, "没有这个 Agent")
    run = agents.run_agent(db, key, body.task, user.username)
    log(db, user, "agent_run", f"{key} #{run.id} {run.status}: {body.task}")
    return run_view(run)


@app.get("/api/agents/runs")
def agent_runs(limit: int = 30, db=Depends(get_db), _: User = Depends(current_user)):
    rows = db.scalars(select(AgentRun).order_by(AgentRun.id.desc()).limit(min(limit, 200)))
    return [run_view(r) for r in rows]


# ---------- 调价审批 ----------
@app.get("/api/pricing/proposals")
def proposals(db=Depends(get_db), _: User = Depends(current_user)):
    rows = db.execute(
        select(PriceProposal, Product).join(Product, PriceProposal.product_id == Product.id)
        .order_by(PriceProposal.status != "pending", PriceProposal.id.desc()).limit(100)
    ).all()
    return [{"id": x.id, "sku": p.sku, "name": p.name, "old_price": x.old_price, "new_price": x.new_price,
             "cost": p.cost, "reason": x.reason, "status": x.status, "reviewed_by": x.reviewed_by,
             "created_at": x.created_at} for x, p in rows]


class ReviewIn(BaseModel):
    approve: bool


@app.post("/api/pricing/proposals/{proposal_id}/review")
def review_proposal(proposal_id: int, body: ReviewIn, db=Depends(get_db), user: User = Depends(require("operator"))):
    proposal = db.get(PriceProposal, proposal_id)
    if not proposal:
        raise HTTPException(404, "调价建议不存在")
    if proposal.status != "pending":
        raise HTTPException(409, "这条建议已经审批过了")
    product = db.get(Product, proposal.product_id)
    if body.approve:
        # 提交后成本可能已变化，批准时再校验一次最低毛利
        min_margin = float(get_setting(db, "min_margin"))
        if (proposal.new_price - product.cost) / proposal.new_price < min_margin:
            raise HTTPException(422, f"该价格的毛利率低于下限 {min_margin:.0%}")
        product.price = proposal.new_price
    proposal.status = "approved" if body.approve else "rejected"
    proposal.reviewed_by = user.username
    log(db, user, "price_review", f"{product.sku} {proposal.old_price}->{proposal.new_price} {proposal.status}")
    return {"status": proposal.status, "price": product.price}


# ---------- 营销 ----------
@app.get("/api/campaigns")
def campaigns(db=Depends(get_db), _: User = Depends(current_user)):
    return [{"id": c.id, "name": c.name, "goal": c.goal, "budget": c.budget, "plan": c.plan, "created_at": c.created_at}
            for c in db.scalars(select(Campaign).order_by(Campaign.id.desc()).limit(50))]


def ab_view(t: ABTest) -> dict:
    return {**ab_stats(t), "variant_a": t.variant_a, "variant_b": t.variant_b, "created_at": t.created_at}


@app.get("/api/ab-tests")
def ab_tests(db=Depends(get_db), _: User = Depends(current_user)):
    return [ab_view(t) for t in db.scalars(select(ABTest).order_by(ABTest.id.desc()).limit(50))]


class ABDataIn(BaseModel):
    a_exposures: int = Field(ge=0)
    a_conversions: int = Field(ge=0)
    b_exposures: int = Field(ge=0)
    b_conversions: int = Field(ge=0)


@app.put("/api/ab-tests/{test_id}/data")
def ab_data(test_id: int, body: ABDataIn, db=Depends(get_db), user: User = Depends(require("operator"))):
    test = db.get(ABTest, test_id)
    if not test:
        raise HTTPException(404, "A/B 测试不存在")
    if body.a_conversions > body.a_exposures or body.b_conversions > body.b_exposures:
        raise HTTPException(422, "转化数不能大于曝光数")
    for field, value in body.model_dump().items():
        setattr(test, field, value)
    log(db, user, "ab_data", f"#{test_id} {body.model_dump()}")
    return ab_view(test)


# ---------- 知识库 ----------
def decode_text(raw: bytes) -> str:
    # Excel 和记事本在国内常存成 GBK
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise HTTPException(422, "无法识别文件编码，请转存为 UTF-8")


@app.get("/api/kb/documents")
def kb_documents(db=Depends(get_db), _: User = Depends(current_user)):
    return [{"id": d.id, "title": d.title, "chunk_count": d.chunk_count, "created_at": d.created_at}
            for d in db.scalars(select(KBDocument).order_by(KBDocument.id.desc()))]


@app.post("/api/kb/documents")
def kb_upload(file: UploadFile = File(...), title: str = Form(""), db=Depends(get_db), user: User = Depends(require("operator"))):
    if not (file.filename or "").lower().endswith((".txt", ".md")):
        raise HTTPException(422, "目前支持 .txt 和 .md 文件")
    try:
        doc = kb.add_document(db, title.strip() or file.filename, decode_text(file.file.read()))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    log(db, user, "kb_upload", doc.title)
    return {"id": doc.id, "title": doc.title, "chunk_count": doc.chunk_count}


@app.delete("/api/kb/documents/{doc_id}")
def kb_delete(doc_id: int, db=Depends(get_db), user: User = Depends(require("operator"))):
    if not kb.delete_document(db, doc_id):
        raise HTTPException(404, "文档不存在")
    log(db, user, "kb_delete", f"#{doc_id}")
    return {"ok": True}


@app.post("/api/kb/ask")
def kb_ask(body: TaskIn, db=Depends(get_db), user: User = Depends(current_user)):
    try:
        result = agents.answer_question(db, body.task)
    except Exception as exc:
        raise HTTPException(502, f"问答失败：{exc}")
    log(db, user, "kb_ask", body.task)
    return result


# ---------- 数据导入 ----------
def _sku_map(db) -> dict[str, int]:
    return {sku: pid for pid, sku in db.execute(select(Product.id, Product.sku))}


def _import_products(db, row, _):
    product = db.scalar(select(Product).where(Product.sku == row["sku"]))
    values = dict(name=row["name"], category=row["category"], cost=float(row["cost"]), price=float(row["price"]),
                  stock=int(row.get("stock") or 0))
    if product:
        for key, value in values.items():
            setattr(product, key, value)
    else:
        db.add(Product(sku=row["sku"], **values))


def _import_orders(db, row, skus):
    db.add(Order(product_id=skus[row["sku"]], quantity=int(row["quantity"]), unit_price=float(row["unit_price"]),
                 order_date=date.fromisoformat(row["order_date"]), segment=row.get("segment") or "未知",
                 region=row.get("region") or "未知"))


def _import_competitors(db, row, skus):
    db.add(CompetitorPrice(product_id=skus[row["sku"]], competitor=row["competitor"], price=float(row["price"]),
                           captured_on=date.fromisoformat(row["captured_on"])))


def _import_trends(db, row, _):
    db.add(MarketTrend(category=row["category"], month=row["month"], search_index=int(row["search_index"]),
                       avg_price=float(row["avg_price"])))


def _import_candidates(db, row, _):
    db.add(Candidate(name=row["name"], category=row["category"], est_cost=float(row["est_cost"]),
                     est_price=float(row["est_price"]), monthly_demand=int(row["monthly_demand"]),
                     growth=float(row["growth"]), competitor_count=int(row["competitor_count"])))


IMPORTERS = {
    "products": _import_products, "orders": _import_orders, "competitor_prices": _import_competitors,
    "market_trends": _import_trends, "candidates": _import_candidates,
}


REQUIRED_COLUMNS = {
    "products": ["sku", "name", "category", "cost", "price"],
    "orders": ["sku", "quantity", "unit_price", "order_date"],
    "competitor_prices": ["sku", "competitor", "price", "captured_on"],
    "market_trends": ["category", "month", "search_index", "avg_price"],
    "candidates": ["name", "category", "est_cost", "est_price", "monthly_demand", "growth", "competitor_count"],
}


@app.post("/api/import/{kind}")
def import_csv(kind: str, file: UploadFile = File(...), db=Depends(get_db), user: User = Depends(require("operator"))):
    if kind not in IMPORTERS:
        raise HTTPException(404, "不支持的数据类型")
    reader = csv.DictReader(io.StringIO(decode_text(file.file.read())))
    columns = {name.strip() for name in reader.fieldnames or [] if name}
    missing = [name for name in REQUIRED_COLUMNS[kind] if name not in columns]
    if missing:
        raise HTTPException(422, f"缺少必填列：{', '.join(missing)}。表头必须是这些英文字段名，顺序不限")
    skus = _sku_map(db)
    count = 0
    # 任一行出错就整批回滚，避免只导入一半
    try:
        for count, row in enumerate(reader, 1):
            IMPORTERS[kind](db, {k.strip(): (v or "").strip() for k, v in row.items() if k}, skus)
            db.flush()
    except KeyError as exc:
        db.rollback()
        raise HTTPException(422, f"第 {count} 行的 SKU {exc} 不存在，请先导入商品。本次已全部撤销")
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, f"第 {count} 行的数字或日期格式不对，本次已全部撤销。数字不要带货币符号，日期写成 2026-01-31（{exc}）")
    if not count:
        raise HTTPException(422, "文件没有数据行")
    log(db, user, "import", f"{kind}: {count} 行")
    return {"imported": count}


# ---------- 系统管理 ----------
class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=8)
    role: str = Field(pattern="^(admin|operator|viewer)$")


class UserPatch(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|operator|viewer)$")
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8)


@app.get("/api/admin/users")
def list_users(db=Depends(get_db), _: User = Depends(require("admin"))):
    return [{"id": u.id, "username": u.username, "role": u.role, "is_active": u.is_active, "created_at": u.created_at}
            for u in db.scalars(select(User).order_by(User.id))]


@app.post("/api/admin/users")
def create_user(body: UserIn, db=Depends(get_db), admin: User = Depends(require("admin"))):
    if db.scalar(select(User.id).where(User.username == body.username)):
        raise HTTPException(409, "用户名已存在")
    user = User(username=body.username, password_hash=hash_password(body.password), role=body.role)
    db.add(user)
    log(db, admin, "user_create", f"{body.username} ({body.role})")
    return {"id": user.id}


@app.patch("/api/admin/users/{user_id}")
def update_user(user_id: int, body: UserPatch, db=Depends(get_db), admin: User = Depends(require("admin"))):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    if user.id == admin.id and (body.is_active is False or (body.role and body.role != "admin")):
        raise HTTPException(422, "不能停用自己或取消自己的管理员权限")
    if body.role:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.password:
        user.password_hash = hash_password(body.password)
    changed = [k for k, v in body.model_dump().items() if v is not None]
    log(db, admin, "user_update", f"{user.username}: {', '.join(changed)}")
    return {"ok": True}


class SettingsIn(BaseModel):
    values: dict[str, str]


@app.get("/api/admin/settings")
def read_settings(db=Depends(get_db), _: User = Depends(require("admin"))):
    return {key: get_setting(db, key) for key in config.DEFAULT_SETTINGS}


@app.put("/api/admin/settings")
def write_settings(body: SettingsIn, db=Depends(get_db), admin: User = Depends(require("admin"))):
    limits = {"min_margin": (0.0, 0.9), "max_price_change": (0.01, 0.5), "agent_max_steps": (1, 12)}
    for key, value in body.values.items():
        if key not in config.DEFAULT_SETTINGS:
            raise HTTPException(422, f"未知配置项：{key}")
        if key in limits:
            try:
                in_range = limits[key][0] <= float(value) <= limits[key][1]
            except ValueError:
                in_range = False
            if not in_range:
                raise HTTPException(422, f"{key} 必须在 {limits[key][0]} 到 {limits[key][1]} 之间")
        elif not value.strip():
            raise HTTPException(422, f"{key} 不能为空")
        db.merge(Setting(key=key, value=value.strip()))
    log(db, admin, "settings_update", str(body.values))
    return {"ok": True}


@app.get("/api/admin/logs")
def logs(limit: int = 100, db=Depends(get_db), _: User = Depends(require("admin"))):
    return [{"id": x.id, "username": x.username, "action": x.action, "detail": x.detail, "created_at": x.created_at}
            for x in db.scalars(select(OpLog).order_by(OpLog.id.desc()).limit(min(limit, 500)))]


@app.get("/")
def index():
    return FileResponse(INDEX_HTML)
