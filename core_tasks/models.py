from dataclasses import dataclass, field
from typing import Dict, List, Optional
import threading


@dataclass
class Variant:
    id: str
    sku: str
    stock: int
    reserved: int = 0


@dataclass
class Product:
    id: str
    title: str
    variants: Dict[str, Variant] = field(default_factory=dict)


@dataclass
class Promotion:
    id: str
    name: str
    stackable: bool = False
    discount_pct: float = 0.0


@dataclass
class Coupon:
    id: str
    code: str
    remaining: int
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def decrement(self) -> bool:
        with self._lock:
            if self.remaining <= 0:
                return False
            self.remaining -= 1
            return True


@dataclass
class AuditEntry:
    id: str
    actor: str
    action: str
    target: str
    diff: Optional[Dict] = None


class InMemoryAuditStore:
    def __init__(self):
        self._entries: List[AuditEntry] = []
        self._lock = threading.Lock()

    def append(self, entry: AuditEntry):
        with self._lock:
            self._entries.append(entry)

    def list(self) -> List[AuditEntry]:
        with self._lock:
            return list(self._entries)
