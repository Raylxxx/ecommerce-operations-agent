"""接口和业务逻辑测试。大模型用假响应代替；数据库默认用临时 SQLite，设置 TEST_DATABASE_URL 可跑在 MySQL 上。"""
import io
import json
import os
import tempfile
from types import SimpleNamespace as NS

os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["JWT_SECRET"] = "test-secret-test-secret-test-secret-123"
os.environ["ADMIN_PASSWORD"] = "test-admin-pass"
os.environ["LLM_API_KEY"] = "fake"

import pytest
from fastapi.testclient import TestClient

from app import agents, kb, seed, tools
from app.main import app
from app.models import Base, Order, PriceProposal, Product, SessionLocal, engine


class FakeLLM:
    """按顺序回放预设的模型响应。"""
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs["messages"], default=str)))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return NS(choices=[NS(message=reply)], usage=NS(total_tokens=10))


def text(content):
    return NS(content=content, tool_calls=None, model_dump=lambda **_: {"role": "assistant", "content": content})


def call(name, args, id="c1"):
    fn = NS(name=name, arguments=json.dumps(args) if isinstance(args, dict) else args)
    calls = [NS(id=id, function=fn)]
    dump = {"role": "assistant", "content": "", "tool_calls": [
        {"id": id, "type": "function", "function": {"name": name, "arguments": fn.arguments}}]}
    return NS(content="", tool_calls=calls, model_dump=lambda **_: dump)


@pytest.fixture(scope="module")
def client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as c:
        with SessionLocal() as db:
            seed.seed(db)
        token = c.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"}).json()["token"]
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


@pytest.fixture()
def db(client):
    with SessionLocal() as session:
        yield session


def test_chat_turns_off_thinking(db, monkeypatch):
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return NS(choices=[NS(message=text("selection"))], usage=NS(total_tokens=1))

    monkeypatch.setattr(agents, "get_client", lambda: NS(chat=NS(completions=NS(create=create))))
    assert agents.route(db, "分析品类") == "selection"
    assert seen["extra_body"] == {"enable_thinking": False}


def test_login_rejects_wrong_password(client):
    assert TestClient(app).post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401


def test_requires_login():
    assert TestClient(app).get("/api/dashboard").status_code == 401


def test_viewer_cannot_run_agent_or_admin(client):
    assert client.post("/api/admin/users", json={"username": "viewer1", "password": "viewer-pass", "role": "viewer"}).status_code == 200
    token = client.post("/api/auth/login", json={"username": "viewer1", "password": "viewer-pass"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    plain = TestClient(app)
    assert plain.get("/api/dashboard", headers=headers).status_code == 200
    assert plain.post("/api/agents/pricing/run", json={"task": "调价"}, headers=headers).status_code == 403
    assert plain.get("/api/admin/logs", headers=headers).status_code == 403


def test_admin_cannot_lock_self_out(client):
    me = next(u for u in client.get("/api/admin/users").json() if u["username"] == "admin")
    assert client.patch(f"/api/admin/users/{me['id']}", json={"is_active": False}).status_code == 422


def test_dashboard(client):
    data = client.get("/api/dashboard").json()
    assert data["demo_data"] is True
    assert data["kpi"]["revenue_30d"] > 0
    assert 0 < data["kpi"]["margin_30d"] < 1
    assert len(data["trend"]) == 90
    assert {c["category"] for c in data["categories"]} == {"数码配件", "厨房电器", "户外运动", "宠物用品"}
    assert len(data["agents"]) == 3


def test_analysis_tools(db):
    growth = {c["category"]: c["growth"] for c in tools.category_analysis(db)}
    assert growth["宠物用品"] > growth["厨房电器"]
    assert tools.recommend_candidates(db, top_n=3)[0]["score"] >= tools.recommend_candidates(db, top_n=3)[2]["score"]
    assert tools.competitor_monitor(db, sku="EAR-001")[0]["competitors"][0]["change_30d"] is not None
    assert tools.market_research(db, category="宠物用品")[0]["demand_growth"] > 0
    audience = tools.audience_analysis(db, category="宠物用品")
    assert abs(sum(s["revenue_share"] for s in audience["by_segment"]) - 1) < 0.01


def test_cost_price_formula(db):
    assert tools.cost_based_price(db, "EAR-001", 0.5)["suggested_price"] == 184.0
    with pytest.raises(ValueError):
        tools.cost_based_price(db, "EAR-001", 1.2)


def test_price_guardrails(db):
    p = db.query(Product).filter_by(sku="EAR-001").one()
    with pytest.raises(ValueError):
        tools.propose_price(db, "EAR-001", p.price * 2, "太高")
    with pytest.raises(ValueError):
        tools.propose_price(db, "EAR-001", p.cost, "低于毛利下限")
    for sku in ("EAR-001", "PWR-001", "PET-003"):
        product = db.query(Product).filter_by(sku=sku).one()
        low, high = tools._guard_range(db, product)
        assert low - 0.01 <= tools.dynamic_price(db, sku)["suggested_price"] <= high + 0.01
        result = tools.optimize_profit(db, sku)
        assert low - 0.01 <= result["best"]["price"] <= high + 0.01
        assert result["best"]["daily_profit"] >= result["current_daily_profit"] - 0.01


def test_elasticity_recovered_from_history(db):
    # 示例数据按 -1.4 的弹性生成，回归结果应接近且不是默认值
    result = tools.optimize_profit(db, "OUT-003")
    assert "回归" in result["method"]
    assert -3 < result["elasticity"] < -0.5


def test_ab_significance():
    make = lambda a, b, c, d: NS(id=1, name="t", a_exposures=a, a_conversions=b, b_exposures=c, b_conversions=d)
    assert "不足" in tools.ab_stats(make(50, 5, 50, 9))["conclusion"]
    significant = tools.ab_stats(make(2000, 100, 2000, 160))
    assert significant["conclusion"].startswith("B") and significant["p_value"] < 0.05
    assert "不显著" in tools.ab_stats(make(1000, 50, 1000, 52))["conclusion"]


def test_kb_search_and_chunks(db):
    hits = kb.search(db, "大促活动价有什么要求")
    assert hits and "大促" in hits[0]["title"]
    assert kb.search(db, "完全无关的量子力学薛定谔") == []
    chunks = kb.split_chunks("段落。" * 400)
    assert len(chunks) > 1 and all(len(c) <= kb.CHUNK_SIZE for c in chunks)
    assert "".join(chunks).replace("\n", "") == "段落。" * 400


def test_agent_runs_tool_then_answers(client):
    fake = FakeLLM([call("dynamic_price", {"sku": "EAR-001"}), text("## 建议\n维持现价")])
    agents._client = fake
    data = client.post("/api/agents/pricing/run", json={"task": "EAR-001 要调价吗"}).json()
    assert data["status"] == "success" and "维持现价" in data["output"]
    assert data["steps"][0]["tool"] == "dynamic_price" and data["tokens"] == 20
    assert fake.calls[1][-1]["role"] == "tool" and "suggested_price" in fake.calls[1][-1]["content"]


def test_agent_tool_errors_go_back_to_model(client):
    for bad in (call("dynamic_price", {"sku": "NOPE"}), call("dynamic_price", "{not json"),
                call("dynamic_price", {"wrong": 1}), call("save_campaign", {"name": "x", "plan": "y"})):
        fake = FakeLLM([bad, text("已说明")])
        agents._client = fake
        data = client.post("/api/agents/pricing/run", json={"task": "测试错误处理"}).json()
        assert data["status"] == "success"
        assert "error" in fake.calls[1][-1]["content"]
    assert not client.get("/api/campaigns").json()  # 定价 Agent 不能调用营销工具


def test_agent_stops_at_step_limit(client):
    client.put("/api/admin/settings", json={"values": {"agent_max_steps": "2"}})
    fake = FakeLLM([call("list_products", {}), call("list_products", {}), text("收尾")])
    agents._client = fake
    data = client.post("/api/agents/pricing/run", json={"task": "循环测试"}).json()
    assert data["status"] == "success" and len(data["steps"]) == 2
    client.put("/api/admin/settings", json={"values": {"agent_max_steps": "6"}})


def test_agent_failure_is_recorded(client):
    agents._client = FakeLLM([RuntimeError("接口超时")])
    data = client.post("/api/agents/selection/run", json={"task": "失败测试"}).json()
    assert data["status"] == "failed" and "接口超时" in data["error"]


def test_auto_route(client):
    agents._client = FakeLLM(["placeholder"])
    agents._client.replies = [text("marketing"), text("文案")]
    assert client.post("/api/agents/auto/run", json={"task": "写个文案"}).json()["agent"] == "marketing"


def test_proposal_requires_human_approval(client, db):
    product = db.query(Product).filter_by(sku="KIT-003").one()
    old, target = product.price, round(product.price * 1.05, 2)
    agents._client = FakeLLM([call("propose_price", {"sku": "KIT-003", "new_price": target, "reason": "测试"}), text("已提交")])
    client.post("/api/agents/pricing/run", json={"task": "提交调价"})
    assert client.get("/api/products").json()[0] and _price(client, "KIT-003") == old  # 提交后价格不变

    pending = next(p for p in client.get("/api/pricing/proposals").json() if p["sku"] == "KIT-003" and p["status"] == "pending")
    assert client.post(f"/api/pricing/proposals/{pending['id']}/review", json={"approve": True}).json()["price"] == target
    assert _price(client, "KIT-003") == target
    assert client.post(f"/api/pricing/proposals/{pending['id']}/review", json={"approve": False}).status_code == 409


def _price(client, sku):
    return next(p["price"] for p in client.get("/api/products").json() if p["sku"] == sku)


def test_marketing_saves_campaign_and_ab(client):
    agents._client = FakeLLM([
        call("save_campaign", {"name": "宠物周", "plan": "方案", "budget": 20000}, "a"),
        call("create_ab_test", {"name": "标题测试", "variant_a": "A文案", "variant_b": "B文案", "sku": "OUT-002"}, "b"),
        text("已保存"),
    ])
    assert client.post("/api/agents/marketing/run", json={"task": "策划并保存"}).json()["status"] == "success"
    assert client.get("/api/campaigns").json()[0]["name"] == "宠物周"
    test = client.get("/api/ab-tests").json()[0]
    bad = {"a_exposures": 10, "a_conversions": 11, "b_exposures": 10, "b_conversions": 1}
    assert client.put(f"/api/ab-tests/{test['test_id']}/data", json=bad).status_code == 422
    good = {"a_exposures": 2000, "a_conversions": 100, "b_exposures": 2000, "b_conversions": 160}
    assert client.put(f"/api/ab-tests/{test['test_id']}/data", json=good).json()["conclusion"].startswith("B")


def test_kb_ask_and_upload(client):
    fake = FakeLLM([text("活动价不得高于最低成交价 [1]")])
    agents._client = fake
    data = client.post("/api/kb/ask", json={"task": "大促活动价有什么要求"}).json()
    assert "[1]" in data["answer"] and data["sources"]
    # 检索不到时不调用模型
    agents._client = FakeLLM([])
    assert client.post("/api/kb/ask", json={"task": "量子力学薛定谔"}).json()["sources"] == []

    files = {"file": ("售后.txt", "退款审核时效为两个工作日。".encode("gbk"))}
    doc = client.post("/api/kb/documents", files=files).json()
    assert doc["chunk_count"] == 1
    assert client.post("/api/kb/documents", files={"file": ("x.exe", b"abc")}).status_code == 422
    assert client.delete(f"/api/kb/documents/{doc['id']}").status_code == 200


def test_csv_import_is_atomic(client, db):
    before = db.query(Order).count()
    bad = "sku,quantity,unit_price,order_date\nEAR-001,1,199,2026-01-01\nNOPE,1,199,2026-01-01\n"
    response = client.post("/api/import/orders", files={"file": ("o.csv", io.BytesIO(bad.encode()))})
    assert response.status_code == 422 and "第 2 行" in response.json()["detail"]
    wrong_header = client.post("/api/import/products", files={"file": ("p.csv", "编号,名称\nA,B\n".encode())})
    assert wrong_header.status_code == 422 and "缺少必填列" in wrong_header.json()["detail"]
    good = "sku,quantity,unit_price,order_date\nEAR-001,1,199,2020-01-01\n"
    assert client.post("/api/import/orders", files={"file": ("o.csv", io.BytesIO(good.encode()))}).json()["imported"] == 1
    db.rollback()  # 结束旧事务，否则 MySQL 的可重复读看不到其他会话刚提交的数据
    assert db.query(Order).count() == before + 1

    products = "sku,name,category,cost,price,stock\nNEW-1,新品,测试,10,20,5\n".encode("gbk")
    assert client.post("/api/import/products", files={"file": ("p.csv", io.BytesIO(products))}).json()["imported"] == 1
    assert _price(client, "NEW-1") == 20


def test_settings_validation_and_logs(client):
    assert client.put("/api/admin/settings", json={"values": {"min_margin": "2"}}).status_code == 422
    assert client.put("/api/admin/settings", json={"values": {"nope": "1"}}).status_code == 422
    assert client.put("/api/admin/settings", json={"values": {"min_margin": "0.12"}}).status_code == 200
    assert client.get("/api/admin/settings").json()["min_margin"] == "0.12"
    assert any(x["action"] == "settings_update" for x in client.get("/api/admin/logs").json())
