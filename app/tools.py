"""Agent 可调用的工具。数字都由这里的代码算出，大模型只负责解读和写作。"""
import math
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
from sqlalchemy import func, select

from . import kb
from .models import ABTest, Campaign, Candidate, CompetitorPrice, MarketTrend, Order, PriceProposal, Product, get_setting

TOOLS: dict[str, dict] = {}


def tool(description: str, properties: dict | None = None, required: list[str] | None = None):
    def wrap(fn):
        TOOLS[fn.__name__] = {
            "fn": fn,
            "schema": {
                "type": "function",
                "function": {
                    "name": fn.__name__,
                    "description": description,
                    "parameters": {"type": "object", "properties": properties or {}, "required": required or []},
                },
            },
        }
        return fn
    return wrap


SKU = {"type": "string", "description": "商品 SKU"}
CATEGORY = {"type": "string", "description": "品类名称，不填表示全部品类"}


def _product(db, sku: str) -> Product:
    product = db.scalar(select(Product).where(Product.sku == sku))
    if not product:
        raise ValueError(f"找不到 SKU：{sku}，可先调用 list_products 查询")
    return product


def _latest_competitors(db, product_id: int) -> list[CompetitorPrice]:
    latest = {}
    rows = db.scalars(
        select(CompetitorPrice).where(CompetitorPrice.product_id == product_id).order_by(CompetitorPrice.captured_on)
    )
    for row in rows:
        latest[row.competitor] = row
    return list(latest.values())


def _anchor_date(db) -> date:
    """以最新订单日期为“今天”，这样历史数据导入后统计窗口依然正确。"""
    return db.scalar(select(func.max(Order.order_date))) or date.today()


def _margin(price: float, cost: float) -> float:
    return round((price - cost) / price, 4) if price else 0.0


@tool("列出商品的 SKU、品类、成本、售价和库存", {"category": CATEGORY})
def list_products(db, category: str | None = None):
    stmt = select(Product).order_by(Product.category, Product.sku)
    if category:
        stmt = stmt.where(Product.category == category)
    return [
        {"sku": p.sku, "name": p.name, "category": p.category, "cost": p.cost, "price": p.price,
         "stock": p.stock, "margin": _margin(p.price, p.cost)}
        for p in db.scalars(stmt)
    ]


@tool("品类分析：各品类的销售额、销量、毛利率，以及后半段相对前半段的增长率",
      {"days": {"type": "integer", "description": "统计天数，默认 90"}})
def category_analysis(db, days: int = 90):
    end = _anchor_date(db)
    start = end - timedelta(days=days - 1)
    mid = start + timedelta(days=days // 2)
    rows = db.execute(
        select(Product.category, Product.cost, Order.quantity, Order.unit_price, Order.order_date)
        .join(Product, Order.product_id == Product.id)
        .where(Order.order_date.between(start, end))
    ).all()
    stats = defaultdict(lambda: {"revenue": 0.0, "units": 0, "profit": 0.0, "early": 0.0, "late": 0.0})
    for r in rows:
        s = stats[r.category]
        revenue = r.quantity * r.unit_price
        s["revenue"] += revenue
        s["units"] += r.quantity
        s["profit"] += r.quantity * (r.unit_price - r.cost)
        s["late" if r.order_date >= mid else "early"] += revenue
    return [
        {"category": c, "revenue": round(s["revenue"], 2), "units": s["units"],
         "gross_margin": round(s["profit"] / s["revenue"], 4) if s["revenue"] else 0,
         "growth": round(s["late"] / s["early"] - 1, 4) if s["early"] else None}
        for c, s in sorted(stats.items(), key=lambda kv: -kv[1]["revenue"])
    ]


@tool("竞品监控：对比本店售价和竞品最新价，列出价差和竞品近 30 天的价格变化",
      {"sku": {**SKU, "description": "商品 SKU，不填则看全部商品"}, "category": CATEGORY})
def competitor_monitor(db, sku: str | None = None, category: str | None = None):
    products = [_product(db, sku)] if sku else [
        p for p in db.scalars(select(Product)) if not category or p.category == category
    ]
    result = []
    for p in products:
        competitors = []
        for row in _latest_competitors(db, p.id):
            old = db.scalar(
                select(CompetitorPrice.price)
                .where(CompetitorPrice.product_id == p.id, CompetitorPrice.competitor == row.competitor,
                       CompetitorPrice.captured_on <= row.captured_on - timedelta(days=30))
                .order_by(CompetitorPrice.captured_on.desc()).limit(1)
            )
            competitors.append({
                "competitor": row.competitor, "price": row.price, "captured_on": str(row.captured_on),
                "change_30d": round(row.price / old - 1, 4) if old else None,
            })
        if competitors:
            avg = sum(c["price"] for c in competitors) / len(competitors)
            result.append({"sku": p.sku, "name": p.name, "our_price": p.price, "competitor_avg": round(avg, 2),
                           "gap_vs_avg": round(p.price / avg - 1, 4), "competitors": competitors})
    return result


@tool("市场调研：品类的月度搜索指数、均价和近 3 个月相对前 3 个月的需求增长", {"category": CATEGORY})
def market_research(db, category: str | None = None):
    stmt = select(MarketTrend).order_by(MarketTrend.month)
    if category:
        stmt = stmt.where(MarketTrend.category == category)
    grouped = defaultdict(list)
    for row in db.scalars(stmt):
        grouped[row.category].append(row)
    result = []
    for c, rows in grouped.items():
        recent, prior = rows[-3:], rows[-6:-3]
        recent_avg = sum(r.search_index for r in recent) / len(recent)
        prior_avg = sum(r.search_index for r in prior) / len(prior) if prior else None
        result.append({
            "category": c, "latest_month": rows[-1].month, "latest_search_index": rows[-1].search_index,
            "latest_avg_price": rows[-1].avg_price,
            "demand_growth": round(recent_avg / prior_avg - 1, 4) if prior_avg else None,
            "series": [{"month": r.month, "search_index": r.search_index} for r in rows[-12:]],
        })
    return result


@tool("选品推荐：按需求增长（40%）、毛利率（35%）、竞争度（25%）给候选商品打分并排序",
      {"category": CATEGORY, "top_n": {"type": "integer", "description": "返回数量，默认 5"}})
def recommend_candidates(db, category: str | None = None, top_n: int = 5):
    stmt = select(Candidate)
    if category:
        stmt = stmt.where(Candidate.category == category)
    rows = list(db.scalars(stmt))
    if not rows:
        return []

    def norm(values):
        lo, hi = min(values), max(values)
        return [(v - lo) / (hi - lo) if hi > lo else 0.5 for v in values]

    growth = norm([r.growth for r in rows])
    margin = norm([_margin(r.est_price, r.est_cost) for r in rows])
    competition = norm([r.competitor_count for r in rows])
    scored = [
        {"name": r.name, "category": r.category, "est_cost": r.est_cost, "est_price": r.est_price,
         "est_margin": _margin(r.est_price, r.est_cost), "monthly_demand": r.monthly_demand, "growth": r.growth,
         "competitor_count": r.competitor_count,
         "score": round(100 * (0.4 * growth[i] + 0.35 * margin[i] + 0.25 * (1 - competition[i])), 1)}
        for i, r in enumerate(rows)
    ]
    return sorted(scored, key=lambda x: -x["score"])[:top_n]


@tool("成本定价：根据成本和目标毛利率计算售价",
      {"sku": SKU, "target_margin": {"type": "number", "description": "目标毛利率，如 0.35"}}, ["sku", "target_margin"])
def cost_based_price(db, sku: str, target_margin: float):
    if not 0 <= target_margin < 1:
        raise ValueError("目标毛利率必须在 0 到 1 之间")
    p = _product(db, sku)
    return {"sku": sku, "cost": p.cost, "current_price": p.price, "target_margin": target_margin,
            "suggested_price": round(p.cost / (1 - target_margin), 2)}


def _guard_range(db, p: Product) -> tuple[float, float]:
    """调价护栏：不低于最低毛利，单次幅度不超过上限。"""
    min_margin = float(get_setting(db, "min_margin"))
    max_change = float(get_setting(db, "max_price_change"))
    return max(p.cost / (1 - min_margin), p.price * (1 - max_change)), p.price * (1 + max_change)


def _daily_sales(db, p: Product, days: int = 30) -> float:
    end = _anchor_date(db)
    sold = db.scalar(
        select(func.sum(Order.quantity))
        .where(Order.product_id == p.id, Order.order_date > end - timedelta(days=days), Order.order_date <= end)
    )
    return float(sold or 0) / days


@tool("动态调价：结合库存周转和竞品价差给出调价建议，结果已受毛利和调价幅度护栏限制", {"sku": SKU}, ["sku"])
def dynamic_price(db, sku: str):
    p = _product(db, sku)
    daily = _daily_sales(db, p)
    days_of_stock = round(p.stock / daily, 1) if daily else None
    competitors = _latest_competitors(db, p.id)
    avg = sum(c.price for c in competitors) / len(competitors) if competitors else None

    adjust, reasons = 0.0, []
    if days_of_stock is not None and days_of_stock < 14:
        adjust += 0.03
        reasons.append(f"库存仅够 {days_of_stock} 天，上调 3%")
    elif days_of_stock is None or days_of_stock > 60:
        adjust -= 0.05
        reasons.append("库存积压（可售超过 60 天或近 30 天无销量），下调 5%")
    if avg:
        gap = p.price / avg - 1
        if gap > 0.05:
            adjust -= 0.02
            reasons.append(f"高于竞品均价 {gap:.1%}，下调 2%")
        elif gap < -0.05:
            adjust += 0.02
            reasons.append(f"低于竞品均价 {-gap:.1%}，上调 2%")
    low, high = _guard_range(db, p)
    suggested = round(min(max(p.price * (1 + adjust), low), high), 2)
    return {"sku": sku, "current_price": p.price, "suggested_price": suggested,
            "suggested_margin": _margin(suggested, p.cost), "daily_sales_30d": round(daily, 2),
            "days_of_stock": days_of_stock, "competitor_avg": round(avg, 2) if avg else None,
            "reasons": reasons or ["库存和价差都在正常范围，维持现价"]}


@tool("利润优化：用历史订单估算价格弹性，在护栏范围内寻找毛利最高的价格；数据不足时使用默认弹性并注明", {"sku": SKU}, ["sku"])
def optimize_profit(db, sku: str):
    p = _product(db, sku)
    rows = db.execute(
        select(Order.order_date, Order.unit_price, func.sum(Order.quantity))
        .where(Order.product_id == p.id).group_by(Order.order_date, Order.unit_price)
    ).all()
    daily = _daily_sales(db, p)
    elasticity, method = -1.5, "默认弹性：历史价格变化不足，结果仅供参考"
    prices = np.array([float(r[1]) for r in rows])
    if len(rows) >= 20 and len(set(prices.round(2))) >= 3 and prices.std() / prices.mean() >= 0.02:
        quantities = np.array([float(r[2]) for r in rows])
        slope = float(np.polyfit(np.log(prices), np.log(quantities), 1)[0])
        elasticity = max(-4.0, min(slope, -0.2))
        method = f"由 {len(rows)} 条按日汇总的订单回归估算；未考虑促销和季节因素"
    low, high = _guard_range(db, p)
    best = None
    for price in np.linspace(low, high, 41):
        quantity = daily * (price / p.price) ** elasticity
        profit = (price - p.cost) * quantity
        if best is None or profit > best["daily_profit"]:
            best = {"price": round(float(price), 2), "daily_units": round(float(quantity), 2),
                    "daily_profit": round(float(profit), 2)}
    return {"sku": sku, "current_price": p.price, "current_daily_profit": round((p.price - p.cost) * daily, 2),
            "best": best, "elasticity": round(elasticity, 3), "method": method,
            "guard_range": [round(low, 2), round(high, 2)]}


@tool("提交调价建议，进入待审批列表。不会直接改价，必须由人工批准",
      {"sku": SKU, "new_price": {"type": "number"}, "reason": {"type": "string", "description": "调价理由"}},
      ["sku", "new_price", "reason"])
def propose_price(db, sku: str, new_price: float, reason: str):
    p = _product(db, sku)
    low, high = _guard_range(db, p)
    if not low - 0.01 <= new_price <= high + 0.01:
        raise ValueError(f"{new_price} 超出允许范围 [{low:.2f}, {high:.2f}]")
    proposal = PriceProposal(product_id=p.id, old_price=p.price, new_price=round(new_price, 2), reason=reason)
    db.add(proposal)
    db.commit()
    return {"proposal_id": proposal.id, "status": "pending", "message": "已提交，等待人工审批"}


@tool("受众分析：按客群和地区统计销售额占比、客单价", {"sku": {**SKU, "description": "商品 SKU，可不填"}, "category": CATEGORY})
def audience_analysis(db, sku: str | None = None, category: str | None = None):
    stmt = select(Order.segment, Order.region, Order.quantity, Order.unit_price).join(Product, Order.product_id == Product.id)
    if sku:
        stmt = stmt.where(Product.sku == sku)
    if category:
        stmt = stmt.where(Product.category == category)
    rows = db.execute(stmt).all()
    total = sum(r.quantity * r.unit_price for r in rows)
    if not total:
        return {"message": "没有符合条件的订单"}

    def group(field):
        amount, count = defaultdict(float), defaultdict(int)
        for r in rows:
            amount[getattr(r, field)] += r.quantity * r.unit_price
            count[getattr(r, field)] += 1
        return [{"name": k, "revenue_share": round(v / total, 4), "avg_order_value": round(v / count[k], 2)}
                for k, v in sorted(amount.items(), key=lambda kv: -kv[1])]

    return {"order_count": len(rows), "revenue": round(total, 2), "by_segment": group("segment"), "by_region": group("region")}


@tool("保存活动方案",
      {"name": {"type": "string"}, "goal": {"type": "string"}, "budget": {"type": "number"},
       "plan": {"type": "string", "description": "完整方案，包含时间、节奏、优惠和渠道"}},
      ["name", "plan"])
def save_campaign(db, name: str, plan: str, goal: str = "", budget: float = 0):
    campaign = Campaign(name=name, goal=goal, budget=budget, plan=plan)
    db.add(campaign)
    db.commit()
    return {"campaign_id": campaign.id, "message": "活动方案已保存"}


@tool("创建 A/B 测试，记录两版文案。真实曝光和转化由店铺或投放平台产生后录入",
      {"name": {"type": "string"}, "variant_a": {"type": "string"}, "variant_b": {"type": "string"},
       "sku": {**SKU, "description": "关联商品 SKU，可不填"}},
      ["name", "variant_a", "variant_b"])
def create_ab_test(db, name: str, variant_a: str, variant_b: str, sku: str | None = None):
    test = ABTest(name=name, variant_a=variant_a, variant_b=variant_b, product_id=_product(db, sku).id if sku else None)
    db.add(test)
    db.commit()
    return {"test_id": test.id, "message": "A/B 测试已创建，请在投放后录入曝光和转化"}


def ab_stats(test: ABTest) -> dict:
    a_rate = test.a_conversions / test.a_exposures if test.a_exposures else 0
    b_rate = test.b_conversions / test.b_exposures if test.b_exposures else 0
    result = {"test_id": test.id, "name": test.name,
              "a": {"exposures": test.a_exposures, "conversions": test.a_conversions, "rate": round(a_rate, 4)},
              "b": {"exposures": test.b_exposures, "conversions": test.b_conversions, "rate": round(b_rate, 4)},
              "p_value": None, "conclusion": "数据不足：每组至少需要 100 次曝光"}
    if test.a_exposures < 100 or test.b_exposures < 100:
        return result
    pooled = (test.a_conversions + test.b_conversions) / (test.a_exposures + test.b_exposures)
    se = math.sqrt(pooled * (1 - pooled) * (1 / test.a_exposures + 1 / test.b_exposures))
    if se == 0:
        result["conclusion"] = "两组转化率相同，无法区分"
        return result
    # 双比例 z 检验
    p_value = math.erfc(abs(b_rate - a_rate) / se / math.sqrt(2))
    result["p_value"] = round(p_value, 4)
    if p_value < 0.05:
        result["conclusion"] = f"{'B' if b_rate > a_rate else 'A'} 版显著更好（p<0.05）"
    else:
        result["conclusion"] = "差异不显著，建议继续收集数据"
    return result


@tool("查询 A/B 测试结果和显著性", {"test_id": {"type": "integer"}}, ["test_id"])
def ab_test_result(db, test_id: int):
    test = db.get(ABTest, test_id)
    if not test:
        raise ValueError(f"找不到 A/B 测试：{test_id}")
    return ab_stats(test)


@tool("检索运营知识库，获取平台规则、运营规范和历史经验", {"query": {"type": "string"}}, ["query"])
def search_knowledge(db, query: str):
    return kb.search(db, query)
