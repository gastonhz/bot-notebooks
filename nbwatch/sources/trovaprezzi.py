"""Trovaprezzi.it: comparador con ofertas de Amazon.it, MediaWorld, Unieuro y tiendas chicas.

Tiene DataDome: solo funciona con el navegador headful. Server-rendered: cuando aparece el
<footer> la página está completa (la de desafío no lo tiene).
"""
import re
from urllib.parse import quote_plus

from selectolax.lexbor import LexborHTMLParser

from ..browser import looks_blocked
from ..models import Listing
from ..util import log, parse_eur, squash
from . import SourceBlocked, SourceError

BASE = "https://www.trovaprezzi.it"
CATEGORY_NOTEBOOK = 10


def _text(node, sel):
    m = node.css_first(sel)
    return squash(m.text(separator=" ")) if m else ""


def _clean(text):
    """Algunos feeds traen '<b>' sin los signos: 'offre b performance /b con…'."""
    return squash(re.sub(r"(?<!\S)/?b(?!\S)", " ", text))


def parse_page(html) -> list[Listing]:
    tree = LexborHTMLParser(html)
    out = []
    for li in tree.css("li.listing_item"):
        offer_id = (li.attributes.get("id") or "").removeprefix("offer-")
        title = _clean(_text(li, ".item_name"))
        if not offer_id or not title:
            continue
        link = li.css_first("a.item_name")
        href = link.attributes.get("href", "") if link else ""
        merchant_link = li.css_first(".merchant_name_and_logo a")
        slug = ""
        if merchant_link:
            slug = (merchant_link.attributes.get("href") or "").split("#")[0].removeprefix("/negozi/")
        rating = None
        rating_node = li.css_first(".rating_image")
        if rating_node:
            m = re.search(r"rate(\d+)", rating_node.attributes.get("class") or "")
            rating = int(m.group(1)) / 10 if m else None
        reviews = None
        for a in li.css("a.merchant_reviews"):
            m = re.fullmatch(r"\(([\d.]+)\)", a.text(strip=True))  # "(5.793)"
            if m:
                reviews = int(m.group(1).replace(".", ""))
        avail_node = li.css_first(".item_availability span")
        avail_class = (avail_node.attributes.get("class") or "") if avail_node else ""
        available = {"available": True, "limited": True, "unavailable": False}.get(avail_class)
        delivery_text = _text(li, ".item_delivery_price")
        shipping = 0.0 if "gratuit" in delivery_text.lower() else parse_eur(delivery_text)
        base_price = parse_eur(_text(li, ".item_basic_price"))
        total = parse_eur(_text(li, ".item_total_price")) or (
            base_price + shipping if base_price is not None and shipping is not None else base_price
        )
        out.append(Listing(
            source="trovaprezzi",
            offer_id=offer_id,
            title=title,
            url=BASE + href if href.startswith("/") else href,
            price=total,
            base_price=base_price,
            shipping=shipping,
            store=_text(li, ".merchant_name") or slug,
            available=available,
            extra={
                "desc": squash(_clean(_text(li, ".item_desc")), 400),
                "store_rating": rating,
                "store_reviews": reviews,
                "review_service": _text(li, ".review_service_logo") or None,
                "payments": [img.attributes.get("alt") for img in li.css(".payment_options img") if img.attributes.get("alt")],
                "availability": avail_node.attributes.get("title") if avail_node else None,
                "store_page": f"{BASE}/negozi/{slug}" if slug else None,
            },
        ))
    return out


def _next_page_url(html, page_num):
    m = re.search(r'href="([^"]*[?&]page=%d\b[^"]*)"' % (page_num + 1), html)
    if not m:
        return None
    href = m.group(1).replace("&amp;", "&")
    return BASE + href if href.startswith("/") else href


def _load(browser, url, partial):
    html, final_url, ready = browser.get(url, "footer")
    items = parse_page(html)
    if not ready and not items:
        marker = looks_blocked(html) or "página incompleta"
        raise SourceBlocked(f"bloqueado ({marker}) en {url}", partial)
    return html, final_url, items


def fetch(ctx) -> list[Listing]:
    cfg = ctx.cfg
    max_pages = ctx.cfg.source_cfg("trovaprezzi").get("max_pages", 5)
    price = cfg["price"]
    browser = ctx.browser()
    found: dict[str, Listing] = {}
    general = 0
    for query, is_mpn in ctx.queries("{} laptop"):
        url = f"{BASE}/categoria.aspx?libera={quote_plus(query)}&id={CATEGORY_NOTEBOOK}"
        html, final_url, items = _load(browser, url, list(found.values()))
        if not is_mpn and "/offerte/" in final_url:
            # listado editorial: se vuelve a pedir con filtro de precio y orden ascendente
            url = f"{final_url.split('?')[0]}?prezzomin={int(price['min_sane'])}&prezzomax={int(price['hard_cap_high_vram'])}&sort=prezzo"
            html, final_url, items = _load(browser, url, list(found.values()))
            page = 1
            while page < max_pages and (nxt := _next_page_url(html, page)):
                page += 1
                html, final_url, more = _load(browser, nxt, list(found.values()))
                items += more
        log.info("trovaprezzi %r: %d ofertas", query, len(items))
        if not is_mpn:
            general += len(items)
        for it in items:
            it.extra["query"] = query
            found.setdefault(it.key, it)
    if general == 0:
        raise SourceError("0 resultados en las búsquedas generales (¿cambió el HTML?)", list(found.values()))
    return list(found.values())
