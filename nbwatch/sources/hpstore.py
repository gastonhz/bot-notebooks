"""HP Store Italia: listado de notebooks gaming (server-rendered, alcanza con HTTP).

Las clases CSS son hasheadas, así que se parsea por estructura: cada link a una ficha de
producto se sube en el DOM hasta el contenedor más grande que contiene un solo producto.
"""
import re

from curl_cffi import requests
from selectolax.lexbor import LexborHTMLParser

from ..models import Listing
from ..util import log, parse_eur, polite_sleep, squash
from . import SourceBlocked, SourceError

LIST_URL = "https://www.hp.com/it-it/shop/listings/laptops?segment=gaming"
PRODUCT_HREF = "/it-it/shop/products/laptops/"
PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})\s*€")
SKU_RE = re.compile(r"\b([A-Z0-9]{6,7})#[A-Z0-9]{3}\b")
CHARGER_MISSING = ("caricabatterie venduto separatamente", "caricabatterie non incluso")
BOILERPLATE = {"IVA incl.", "VEDI PRODOTTO", "AGGIUNGI AL CARRELLO", "Offerta", "Con codice",
               "Confronta modelli", "Passa al confronto"}
NOT_AVAILABLE = ("non disponibile", "esaurito", "prossimamente", "disponibile a breve", "non acquistabile")


def _product_links(node):
    return {a.attributes.get("href", "").split("?")[0] for a in node.css(f'a[href*="{PRODUCT_HREF}"]')}


def parse_page(html) -> list[Listing]:
    tree = LexborHTMLParser(html)
    seen = {}
    for a in tree.css(f'a[href*="{PRODUCT_HREF}"]'):
        href = a.attributes.get("href", "").split("?")[0]
        if href in seen:
            continue
        tile = a
        while tile.parent is not None and len(_product_links(tile.parent)) == 1:
            tile = tile.parent
        parts = [p.strip() for p in tile.text(separator="|").split("|") if p.strip()]
        text = " | ".join(parts)
        prices = [parse_eur(p) for p in PRICE_RE.findall(text)]
        if not prices:
            continue  # link suelto (carrusel, comparador), no una tarjeta con precio
        names = [squash(x.text()) for x in tile.css(f'a[href*="{PRODUCT_HREF}"]')]
        name = max(names, key=len) if any(names) else parts[0]
        sku_m = SKU_RE.search(text) or SKU_RE.search(href.upper().replace("-ABZ", "#ABZ"))
        low = text.lower()
        if any(k in low for k in NOT_AVAILABLE):
            available = False
        elif "disponibile" in low:
            available = True
        else:
            available = None
        coupon = None
        if "Con codice" in parts:
            i = parts.index("Con codice")
            coupon = parts[i + 1] if i + 1 < len(parts) else None
        specs = [p for p in parts if p != name and p != coupon and not PRICE_RE.search(p) and p not in BOILERPLATE]
        seen[href] = Listing(
            source="hpstore",
            offer_id=(sku_m.group(1) if sku_m else href.rsplit("/", 1)[-1]),
            title=name,
            url="https://www.hp.com" + href if href.startswith("/") else href,
            price=min(prices),
            base_price=max(prices),
            shipping=None,
            store="HP Store",
            available=available,
            mpn=sku_m.group(1) if sku_m else None,
            extra={
                "specs": squash(" | ".join(specs), 500),
                "coupon": coupon,
                "charger_separate": any(k in low for k in CHARGER_MISSING),
            },
        )
    return list(seen.values())


def fetch(ctx) -> list[Listing]:
    max_pages = ctx.cfg.source_cfg("hpstore").get("max_pages", 8)
    session = requests.Session(impersonate="chrome")
    found: dict[str, Listing] = {}
    for page in range(1, max_pages + 1):
        if page > 1:
            polite_sleep(3, 7)
        url = LIST_URL if page == 1 else f"{LIST_URL}&page={page}"
        try:
            r = session.get(url, timeout=40, headers={"Accept-Language": "it-IT,it;q=0.9"})
        except Exception as e:
            raise SourceError(f"error de red: {e}", list(found.values()))
        if r.status_code in (403, 429):
            raise SourceBlocked(f"HTTP {r.status_code} en {url}", list(found.values()))
        if r.status_code != 200:
            raise SourceError(f"HTTP {r.status_code} en {url}", list(found.values()))
        items = parse_page(r.text)
        new = [it for it in items if it.key not in found]
        log.info("hpstore página %d: %d productos (%d nuevos)", page, len(items), len(new))
        if not new:
            break
        for it in new:
            found[it.key] = it
    if not found:
        raise SourceError("0 productos en el listado gaming (¿cambió el HTML?)")
    return list(found.values())
