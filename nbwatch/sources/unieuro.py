"""Unieuro.it vía el índice Algolia que usa su propio buscador (JSON, sin navegador).

La clave es la "search-only" pública que el sitio entrega a cualquier navegador. Si la rotan,
la fuente empieza a fallar con 403 y llega el aviso de salud: hay que tomar la nueva de las
peticiones a *.algolia.net que hace https://www.unieuro.it/online/products?q=rtx (DevTools).
"""
import re

from curl_cffi import requests

from ..models import Listing
from ..util import log, polite_sleep, squash
from . import SourceBlocked, SourceError

ALGOLIA_URL = "https://mnbcenyfii-dsn.algolia.net/1/indexes/*/queries"
APP_ID = "MNBCENYFII"
API_KEY = "977ed8d06b718d4929ca789c78c4107a"
INDEX = "sgmproducts_prod"
BASE = "https://www.unieuro.it/online"


def _is_notebook(hit):
    return any("Notebook" in (c.get("lvl3") or "") for c in hit.get("categoryNames_it") or [])


def to_listing(hit):
    home = ((hit.get("leadTimeSection") or {}).get("homeSection")) or {}
    stock = hit.get("stockLevelStatus")
    # sin descuento no viene discountedPrice: el precio vigente queda en facetPrice/originalPrice
    price = hit.get("discountedPrice") or hit.get("facetPrice") or hit.get("originalPrice")
    cats = {c.get("lvl3") or "" for c in hit.get("categoryNames_it") or []}
    return Listing(
        source="unieuro",
        offer_id=hit["code"],
        title=squash(hit.get("title_it")),
        url=BASE + (hit.get("productUrl_it") or ""),
        price=price,
        base_price=price,
        shipping=0.0 if hit.get("hasFreeDelivery") else None,
        store="Unieuro",
        available=bool(hit.get("isAvailable")) and stock != "outOfStock" and not hit.get("preOrder"),
        mpn=(hit.get("manufacturerAID") or "").split("#")[0] or None,
        extra={
            "specs": hit.get("featuresSummary_it") or {},
            "stock": stock,
            "delivery": home.get("leadTimeDates"),
            "delivery_cost": home.get("deliveryCost"),
            "original_price": hit.get("originalPrice"),
            "slim": any("Slim" in c for c in cats),
            "promo": squash(re.sub(r"<[^>]+>", " ", hit.get("primaryPromotionTitle") or "")) or None,
        },
    )


def fetch(ctx) -> list[Listing]:
    session = requests.Session(impersonate="chrome")
    headers = {
        "x-algolia-application-id": APP_ID,
        "x-algolia-api-key": API_KEY,
        "Origin": "https://www.unieuro.it",
        "Referer": "https://www.unieuro.it/",
    }
    found: dict[str, Listing] = {}
    general = 0
    for i, (query, is_mpn) in enumerate(ctx.queries()):
        if i:
            polite_sleep(2, 5)
        body = {"requests": [{"indexName": INDEX, "query": query, "hitsPerPage": 100, "page": 0}]}
        try:
            r = session.post(ALGOLIA_URL, json=body, headers=headers, timeout=30)
        except Exception as e:
            raise SourceError(f"error de red: {e}", list(found.values()))
        if r.status_code in (401, 403, 429):
            raise SourceBlocked(f"Algolia respondió {r.status_code} (¿clave rotada?)", list(found.values()))
        if r.status_code != 200:
            raise SourceError(f"Algolia respondió {r.status_code}", list(found.values()))
        hits = r.json()["results"][0].get("hits", [])
        items = [to_listing(h) for h in hits if _is_notebook(h) and h.get("code")]
        log.info("unieuro %r: %d notebooks (%d hits)", query, len(items), len(hits))
        if not is_mpn:
            general += len(items)
        for it in items:
            it.extra["query"] = query
            found.setdefault(it.key, it)
    if general == 0:
        raise SourceError("0 resultados en las búsquedas generales", list(found.values()))
    return list(found.values())
