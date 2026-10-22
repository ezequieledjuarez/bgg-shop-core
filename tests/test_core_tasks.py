import threading
from core_tasks.models import Variant, Product, Promotion, Coupon, InMemoryAuditStore
from core_tasks.services import StockService, PromotionEvaluator, CouponService, EventPublisher


def test_adjust_stock_allows_valid_update_and_audit():
    audit = InMemoryAuditStore()
    stock_svc = StockService(audit)
    v = Variant(id="v1", sku="SKU1", stock=10, reserved=3)
    assert stock_svc.adjust_stock(v, -5, actor="tester") is True
    assert v.stock == 5
    entries = audit.list()
    assert len(entries) == 1
    assert entries[0].action == "adjust_stock"


def test_adjust_stock_rejects_when_below_reserved():
    audit = InMemoryAuditStore()
    stock_svc = StockService(audit)
    v = Variant(id="v2", sku="SKU2", stock=5, reserved=4)
    assert stock_svc.adjust_stock(v, -2, actor="tester") is False
    assert v.stock == 5
    assert audit.list() == []


def test_promotion_evaluator_stackable_semantics():
    p1 = Promotion(id="p1", name="Ten", stackable=False, discount_pct=10)
    p2 = Promotion(id="p2", name="Five", stackable=True, discount_pct=5)
    evalr = PromotionEvaluator([p1, p2])
    # apply both; non-stackable p1 should combine with stackable p2
    price = evalr.evaluate(100.0, ["p1", "p2"])
    # 100 -> after p1 90 -> after p2 85.5 -> rounded 85.5
    assert price == 85.5


def test_coupon_decrement_atomic_and_audit():
    audit = InMemoryAuditStore()
    c = Coupon(id="c1", code="OFF10", remaining=2)
    svc = CouponService([c], audit)

    results = []

    def worker():
        results.append(svc.apply_coupon("OFF10", actor="u"))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Only two should succeed
    assert sum(1 for r in results if r) == 2
    entries = audit.list()
    assert len(entries) == 4


def test_event_publisher_records_events():
    pub = EventPublisher()
    pub.publish("product.updated", {"id": "p1"})
    assert len(pub.published) == 1
