"""写入虚构的示例数据，方便上手体验。

python -m app.seed          写入示例数据（已有商品时跳过）
python -m app.seed --clear  清空全部业务数据，保留账号和配置
"""
import math
import random
import sys
from datetime import date, timedelta

from sqlalchemy import delete, select

from . import kb
from .models import (ABTest, AgentRun, Base, Campaign, Candidate, CompetitorPrice, KBChunk, KBDocument, MarketTrend,
                     Order, PriceProposal, Product, SessionLocal, Setting, engine)

PRODUCTS = [
    ("EAR-001", "降噪蓝牙耳机 Pro", "数码配件", 92, 199, 620),
    ("EAR-002", "运动挂耳耳机", "数码配件", 48, 99, 1500),
    ("PWR-001", "20000mAh 快充充电宝", "数码配件", 65, 129, 95),
    ("KIT-001", "多功能空气炸锅 5L", "厨房电器", 168, 329, 310),
    ("KIT-002", "便携榨汁杯", "厨房电器", 42, 89, 2600),
    ("KIT-003", "恒温电热水壶", "厨房电器", 58, 139, 480),
    ("OUT-001", "轻量折叠露营椅", "户外运动", 55, 119, 720),
    ("OUT-002", "防晒冰感外套", "户外运动", 38, 109, 1300),
    ("OUT-003", "便携野餐垫", "户外运动", 21, 59, 2100),
    ("PET-001", "智能自动喂食器", "宠物用品", 128, 269, 180),
    ("PET-002", "猫咪饮水机", "宠物用品", 46, 109, 560),
    ("PET-003", "宠物除毛梳", "宠物用品", 9, 35, 3100),
]
DAILY_DEMAND = {"EAR-001": 9, "EAR-002": 15, "PWR-001": 12, "KIT-001": 6, "KIT-002": 7, "KIT-003": 9,
                "OUT-001": 10, "OUT-002": 16, "OUT-003": 18, "PET-001": 5, "PET-002": 9, "PET-003": 24}
GROWTH = {"数码配件": 0.0, "厨房电器": -0.25, "户外运动": 0.35, "宠物用品": 0.45}
SEGMENTS = [("新客", .35), ("复购客", .3), ("学生党", .15), ("家庭用户", .2)]
REGIONS = [("华东", .36), ("华南", .24), ("华北", .18), ("西南", .12), ("其他", .10)]
COMPETITORS = ["甲店", "乙店", "丙店"]
CANDIDATES = [
    ("宠物烘干箱", "宠物用品", 210, 459, 3800, 0.42, 16),
    ("猫爬架（小户型）", "宠物用品", 75, 189, 8200, 0.18, 54),
    ("宠物定位项圈", "宠物用品", 62, 169, 2600, 0.61, 11),
    ("车载露营冰箱", "户外运动", 260, 499, 2400, 0.35, 22),
    ("超轻登山杖", "户外运动", 34, 99, 9100, 0.27, 48),
    ("便携咖啡手冲套装", "户外运动", 48, 139, 4300, 0.51, 19),
    ("磁吸无线充电宝", "数码配件", 72, 149, 12500, 0.12, 120),
    ("桌面收纳扩展坞", "数码配件", 88, 199, 3100, 0.22, 34),
    ("迷你破壁机", "厨房电器", 95, 199, 5200, -0.08, 76),
    ("一人食电煮锅", "厨房电器", 39, 89, 7400, 0.05, 88),
]
DOCS = [
    ("示例：广告文案规范", """商品文案禁止使用绝对化用语，例如“最好”“第一”“国家级”“顶级”。

涉及功效的描述需要有依据。没有检测报告时，不得宣称“100%防晒”“永不损坏”等效果。

价格宣传中的“原价”必须是前七日内实际成交过的最低价以上的真实价格，不得虚构。

用户评价截图需要获得授权，并且不能修改原文内容。"""),
    ("示例：大促活动规则", """参加大促的商品，活动价不得高于报名前 30 天的最低成交价。

大促分为预热期和爆发期：预热期 5 天，以种草和加购为主；爆发期 3 天，以优惠券和满减收割为主。

报名大促前，核心商品的库存应至少覆盖预计销量的 1.5 倍。

大促期间改价需要提前一个工作日报备，爆发期内原则上不改价。"""),
    ("示例：定价与售后手册", """店铺常规商品的毛利率目标不低于 35%，引流款可以放宽到 15%，但引流款数量不超过在售商品的 20%。

竞品价格变化超过 10% 时，需要在两个工作日内评估是否跟进，不要自动跟价到亏损。

七天无理由退货由店铺承担退货运费险。开封后影响二次销售的商品，需要在详情页提前说明。

库存可售天数低于 14 天时优先补货，高于 60 天时考虑促销清库存。"""),
]


def _pick(rng, options):
    return rng.choices([o[0] for o in options], [o[1] for o in options])[0]


def seed(db) -> bool:
    if db.scalar(select(Product.id).limit(1)):
        return False
    rng = random.Random(42)
    today = date.today()
    products = [Product(sku=s, name=n, category=c, cost=co, price=p, stock=st) for s, n, c, co, p, st in PRODUCTS]
    db.add_all(products)
    db.flush()

    orders, competitor_rows = [], []
    for p in products:
        demand, growth = DAILY_DEMAND[p.sku], GROWTH[p.category]
        for days_ago in range(179, -1, -1):
            day = today - timedelta(days=days_ago)
            # 分段调价，让价格弹性有可估算的历史
            price = round(p.price * (1 + 0.07 * math.sin((days_ago // 21) * 1.7)), 2)
            trend = 1 + growth * (179 - days_ago) / 180
            weekend = 1.25 if day.weekday() >= 5 else 1
            expected = demand * trend * weekend * (price / p.price) ** -1.4
            for _ in range(max(0, round(rng.gauss(expected, expected * 0.2)))):
                orders.append(Order(product_id=p.id, quantity=rng.choices([1, 2], [.85, .15])[0], unit_price=price,
                                    segment=_pick(rng, SEGMENTS), region=_pick(rng, REGIONS), order_date=day))
        for name in COMPETITORS:
            base = p.price * rng.uniform(0.86, 1.1)
            for weeks_ago in range(12, -1, -1):
                competitor_rows.append(CompetitorPrice(
                    product_id=p.id, competitor=name, captured_on=today - timedelta(weeks=weeks_ago),
                    price=round(base * (1 - 0.006 * (12 - weeks_ago)) * rng.uniform(0.99, 1.01), 2)))
    db.add_all(orders)
    db.add_all(competitor_rows)

    first_of_month = today.replace(day=1)
    for category, growth in GROWTH.items():
        base = rng.randint(4000, 9000)
        for offset in range(11, -1, -1):
            month = (first_of_month - timedelta(days=offset * 30)).strftime("%Y-%m")
            db.add(MarketTrend(category=category, month=month,
                               search_index=int(base * (1 + growth * (11 - offset) / 11) * rng.uniform(.96, 1.04)),
                               avg_price=round(rng.uniform(90, 220), 2)))
    db.add_all(Candidate(name=n, category=c, est_cost=co, est_price=p, monthly_demand=d, growth=g, competitor_count=k)
               for n, c, co, p, d, g, k in CANDIDATES)
    for title, text in DOCS:
        kb.add_document(db, title, text)
    db.merge(Setting(key="demo_data", value="1"))
    db.commit()
    return True


def clear(db) -> None:
    for model in (KBChunk, KBDocument, ABTest, Campaign, PriceProposal, CompetitorPrice, Order, Candidate, MarketTrend,
                  Product, AgentRun):
        db.execute(delete(model))
    db.execute(delete(Setting).where(Setting.key == "demo_data"))
    db.commit()


if __name__ == "__main__":
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        if "--clear" in sys.argv:
            clear(session)
            print("业务数据已清空")
        else:
            print("示例数据已写入" if seed(session) else "已有商品数据，跳过")
