from dataclasses import dataclass, field


@dataclass
class Listing:
    source: str
    offer_id: str
    title: str
    url: str
    price: float | None          # total con envío cuando se conoce
    store: str
    available: bool | None
    base_price: float | None = None
    shipping: float | None = None
    mpn: str | None = None       # cuando la fuente lo da explícito (Unieuro, HP Store)
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.offer_id}"
