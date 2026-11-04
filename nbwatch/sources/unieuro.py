"""Unieuro.it vía el índice Algolia que usa su propio buscador (JSON, sin navegador).

La clave es la "search-only" pública que el sitio entrega a cualquier navegador. Si la rotan,
la fuente empieza a fallar con 403 y llega el aviso de salud: hay que tomar la nueva de las
peticiones a *.algolia.net que hace https://www.unieuro.it/online/products?q=rtx (DevTools).

El stock del índice se actualiza con retraso (puede decir inStock cuando la ficha ya dice "Non
disponibile"), así que lo que importa se verifica en la ficha: ver verify_stock.
"""
import re

from curl_cffi import requests

from ..models import Listing
from ..prefilter import evaluate
from ..util import log, parse_eur, polite_sleep, squash
from . import SourceBlocked, SourceError

ALGOLIA_URL = "https://mnbcenyfii-dsn.algolia.net/1/indexes/*/queries"
APP_ID = "MNBCENYFII"
API_KEY = "977ed8d06b718d4929ca789c78c4107a"
INDEX = "sgmproducts_prod"
BASE = "https://www.unieuro.it/online"
# en el JSON que la ficha trae embebido: "stock":{"stockLevel":0,"stockLevelStatus":"outOfStock",...}
STOCK_RE = re.compile(r'"stockLevelStatus"\s*:\s*"(\w+)"')


def _is_notebook(hit):
    return any("Notebook" in (c.get("lvl3") or "") for c in hit.get("categoryNames_it") or [])


def _strip_tags(html):
    return squash(re.sub(r"<[^>]+>", " ", html or ""))


def badges(hit) -> list[str]:
    """Etiquetas de promoción de la ficha ("OUTLET ONLINE"), no solo la principal.

    allPromotions trae "PO-X|Promo Online|OUTLET|07-10-2025_31-12-2099|OUTLET|OUTLET ONLINE_promotion";
    las entradas sin "|" ("vslp_promotion") son códigos internos.
    """
    out = [_strip_tags(hit.get("primaryPromotionTitle"))]
    out += [p.rsplit("|", 1)[-1].removesuffix("_promotion") for p in hit.get("allPromotions") or [] if "|" in p]
    return [b for b in dict.fromkeys(out) if b]


def to_listing(hit):
    home = ((hit.get("leadTimeSection") or {}).get("homeSection")) or {}
    stock = hit.get("stockLevelStatus")
    # sin descuento no viene discountedPrice: el precio vigente queda en facetPrice/originalPrice
    base = hit.get("discountedPrice") or hit.get("facetPrice") or hit.get("originalPrice")
    cost = home.get("deliveryCost") or ""
    shipping = 0.0 if hit.get("hasFreeDelivery") or "gratuit" in cost.lower() else parse_eur(cost)
    cats = {c.get("lvl3") or "" for c in hit.get("categoryNames_it") or []}
    return Listing(
        source="unieuro",
        offer_id=hit["code"],
        title=squash(hit.get("title_it")),
        url=BASE + (hit.get("productUrl_it") or ""),
        price=base + shipping if base is not None and shipping is not None else base,
        base_price=base,
        shipping=shipping,
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
            "promo": _strip_tags(hit.get("primaryPromotionTitle")) or None,
            "badges": badges(hit),
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
    verify_stock(session, ctx, list(found.values()))
    return list(found.values())


def page_stock(session, url) -> str | None:
    """stockLevelStatus de la ficha (inStock / lowStock / outOfStock), o None si no se pudo leer."""
    try:
        r = session.get(url, timeout=40, headers={"Accept-Language": "it-IT,it;q=0.9"})
    except Exception as e:
        log.warning("unieuro: no pude abrir %s: %s", url, e)
        return None
    m = STOCK_RE.search(r.text) if r.status_code == 200 else None
    return m.group(1) if m else None


def verify_stock(session, ctx, listings):
    """Corrige con la ficha lo que Algolia da como disponible y pasa el prefiltro o es un MPN vigilado
    (las alertas de la referencia no dependen del prefiltro). El resto no hace falta: no llega a nada."""
    mpns = [q.lower() for q, is_mpn in ctx.queries() if is_mpn]
    todo = [l for l in listings if l.available and (
        any(m in f"{l.title} {l.mpn or ''} {l.url}".lower() for m in mpns) or evaluate(l.as_row(), ctx.cfg).passed)]
    gone = unknown = 0
    for l in todo:
        polite_sleep(2, 5)
        status = page_stock(session, l.url)
        if status is None:
            unknown += 1
            l.extra["stock_unverified"] = True
            continue
        l.extra["stock"] = status
        if status == "outOfStock":
            l.available = False
            gone += 1
    log.info("unieuro: stock verificado en %d fichas: %d agotadas aunque Algolia las daba disponibles, "
             "%d sin verificar", len(todo), gone, unknown)
    if todo and unknown == len(todo):
        raise SourceError("no pude leer el stock en ninguna ficha (¿cambió la página?); quedan los datos de Algolia",
                          listings)
