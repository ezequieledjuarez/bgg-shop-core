from typing import List, Optional
from .models import Product, Variant, Promotion, Coupon, InMemoryAuditStore, AuditEntry
import threading


class StockService:
    def __init__(self, audit: InMemoryAuditStore):
        self._lock = threading.Lock()
        self.audit = audit

    def adjust_stock(self, variant: Variant, delta: int, actor: str) -> bool:
        """Adjust stock ensuring stock - reserved >= 0 after change.

        Returns True if applied, False if rejected.
        """
        with self._lock:
            new_stock = variant.stock + delta
            if new_stock < variant.reserved:
                return False
            old = variant.stock
            variant.stock = new_stock
            self.audit.append(AuditEntry(id=f"audit-{variant.id}", actor=actor, action="adjust_stock", target=variant.sku, diff={"old": old, "new": new_stock}))
            return True


class PromotionEvaluator:
    def __init__(self, promotions: List[Promotion]):
        self.promotions = promotions

    def evaluate(self, price: float, applied: List[str]) -> float:
        """Apply promotions according to stackable semantics.
        `applied` is list of promotion ids to apply.
        """
        total = price
        for p in self.promotions:
            if p.id not in applied:
                continue
            if not p.stackable and any((not self._is_stackable(pid) for pid in applied if pid != p.id)):
                # skip non-stackable if other non-stackable is applied
                # but allow stacking with stackable promotions
                # simplified: if any other non-stackable present, skip this
                continue
            total = total * (1.0 - p.discount_pct / 100.0)
        return round(total, 2)

    def _is_stackable(self, pid: str) -> bool:
        for p in self.promotions:
            if p.id == pid:
                return p.stackable
        return False


class CouponService:
    def __init__(self, coupons: List[Coupon], audit: InMemoryAuditStore):
        self.coupons = {c.code: c for c in coupons}
        self.audit = audit

    def apply_coupon(self, code: str, actor: str) -> bool:
        c = self.coupons.get(code)
        if not c:
            return False
        ok = c.decrement()
        self.audit.append(AuditEntry(id=f"audit-coupon-{code}", actor=actor, action="apply_coupon", target=code, diff={"success": ok, "remaining": c.remaining}))
        return ok


class EventPublisher:
    def __init__(self):
        self.published = []

    def publish(self, event_type: str, payload: dict):
        self.published.append((event_type, payload))


class AuditFacade:
    def __init__(self):
        self.store = InMemoryAuditStore()

    def log(self, entry: AuditEntry):
        self.store.append(entry)
