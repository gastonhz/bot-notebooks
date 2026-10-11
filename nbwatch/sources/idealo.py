"""Idealo.it: segundo comparador. Los resultados vienen como JSON (base64) embebido en la página.

Cada resultado es un producto con su oferta más barata (tienda + envío). Funciona con el navegador.
"""
import base64
import json
import re
from urllib.parse import quote_plus

from ..browser import looks_blocked
from ..models import Listing
from ..util import log, parse_eur, squash
from . import SourceBlocked, SourceError

CATEGORY_NOTEBOOK = "3751"
READY = 'script[data-csr-component="MainSearchResult"]'
DATA_RE = re.compile(r"initialData\['searchResult'\]\s*=\s*\"([A-Za-z0-9+/=]+)\"")


def parse_page(html):
    """Lista de Listing, o None si la página no trae el bloque de datos esperado."""
    m = DATA_RE.search(html)
    if not m:
        return None
    data = json.loads(base64.b64decode(m.group(1)))
    out = []
    for it in data.get("items", []):
        if it.get("categoryId") != CATEGORY_NOTEBOOK or it.get("type") not in ("PRODUCT", "OFFER"):
            continue
        offer = it.get("offerInfo") or {}
        shop = it.get("shopInfo") or {}
        base_price = it.get("rawPrice") or parse_eur(offer.get("formattedPrice"))
        shipping = parse_eur(shop.get("shippingCosts"))
        if offer.get("usedOnly"):
            continue
        out.append(Listing(
            source="idealo",
            offer_id=str(it.get("id") or it.get("offerId")),
            title=squash(it.get("title")),
            url=it.get("href") or offer.get("leadoutHref") or "",
            price=(base_price + shipping) if base_price is not None and shipping is not None else base_price,
            base_price=base_price,
            shipping=shipping,
            store=shop.get("shopName") or "?",
            available=True,  # idealo solo lista ofertas vigentes
            extra={
                "specs": it.get("characteristics") or [],
                "offer_count": offer.get("offerCount"),
                "note": "precio = oferta más barata en idealo; la tienda puede variar",
                "delivery": shop.get("deliveryStatus") or shop.get("deliveryInformation"),
            },
        ))
    return out


def fetch(ctx) -> list[Listing]:
    browser = ctx.browser()
    found: dict[str, Listing] = {}
    general = 0
    for query, is_mpn in ctx.queries("notebook {}"):
        url = f"https://www.idealo.it/risultati.html?q={quote_plus(query)}"
        html, _, ready = browser.get(url, READY)
        items = parse_page(html)
        if items is None:
            marker = looks_blocked(html)
            if marker or not ready:
                raise SourceBlocked(f"bloqueado ({marker or 'sin datos'}) en {url}", list(found.values()))
            raise SourceError(f"la página no trae el JSON de resultados (¿cambió la estructura?) en {url}", list(found.values()))
        log.info("idealo %r: %d productos", query, len(items))
        if not is_mpn:
            general += len(items)
        for it in items:
            it.extra["query"] = query
            found.setdefault(it.key, it)
    if general == 0:
        raise SourceError("0 resultados en las búsquedas generales", list(found.values()))
    return list(found.values())
