"""Filtro determinístico previo al juez: GPU, CPU, precio, disponibilidad, tienda, palabras excluidas."""
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .util import today_rome

GPU_RE = re.compile(r"RTX\W{0,3}(\d{4})(\s?Ti\b)?", re.I)
CPU_TIERS = ("tier1", "tier2", "tier3", "avoid")
RAM_SIZES = {8, 12, 16, 24, 32, 48, 64, 96, 128}
# RAM dicha explícitamente: "32 GB DDR5", "RAM: 16 GB", "16GB RAM", "Memoria Interna: 32 GB" (Unieuro)
RAM_RES = [
    re.compile(r"\b(\d{1,3})\s*GB\b[^,;|]{0,12}?\b(?:LP)?DDR\d", re.I),
    re.compile(r"\b(?:RAM|Memoria Interna)\b\W{0,3}(?:(?:LP)?DDR\d\w*\W{0,3})?(\d{1,3})\s*GB", re.I),
    re.compile(r"\b(\d{1,3})\s*GB\s*(?:di\s+)?RAM\b", re.I),
]
# VRAM, para que "RTX 5070 Ti 12GB" o "Memoria Grafica Dedicata: 12 GB" no cuenten como RAM
VRAM_RE = re.compile(r"(?:RTX\W{0,3}\d{4}(?:\s?Ti)?|GDDR\d\w*|VRAM|Grafica[^,;|]{0,20}?)\W{0,3}\d{1,2}\s*GB"
                     r"|\b\d{1,2}\s*GB\W{0,3}(?:GDDR\d\w*|VRAM)", re.I)
# "15/11", "15/11/2026", "15-11-2026"; no "2-4 giorni" ni "24/48 ore"
DATE_RE = re.compile(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\b(?!\s*(?:giorni|gg|ore|h)\b)"
                     r"|\b(\d{1,2})-(\d{1,2})-(\d{4})\b")
MONTHS = {"gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6, "lug": 7, "ago": 8, "set": 9, "ott": 10,
          "nov": 11, "dic": 12}
MPN_RES = [
    re.compile(r"\b[A-Z0-9]{5}EA\b"),                       # HP (C9SE3EA)
    re.compile(r"\b90NR[0-9A-Z]{4}-M[0-9A-Z]{5}\b"),        # ASUS
    re.compile(r"\b9S7-[0-9A-Z]{6}-[0-9A-Z]{3}\b"),         # MSI
    re.compile(r"\bNH\.[A-Z0-9]{5}\.[A-Z0-9]{3}\b"),        # Acer
    re.compile(r"\b8[0-9][A-Z0-9]{2}00[A-Z0-9]{4}\b"),      # Lenovo (83DG00ABIX)
]


def _field(row, name):
    return row[name] if name in row.keys() else None


def row_extra(row) -> dict:
    extra = _field(row, "extra")
    return json.loads(extra) if isinstance(extra, str) else (extra or {})


def spec_text(row) -> str:
    extra = row_extra(row)
    specs = extra.get("specs")
    if isinstance(specs, dict):
        specs = " ".join(f"{k}: {v}" for k, v in specs.items())
    elif isinstance(specs, list):
        specs = " ".join(specs)
    return f"{specs or ''} {extra.get('desc') or ''}"


def detect_gpu(text: str) -> str | None:
    m = GPU_RE.search(text or "")
    if not m:
        return None
    return m.group(1) + (" Ti" if m.group(2) else "")


def normalize_cpu_text(text: str) -> str:
    """'Intel® Core™ Ultra 7 - 255H' -> 'Intel Core Ultra 7 255H' (HP Store separa familia y modelo con ' - ')."""
    text = re.sub(r"[™®©]|\((?:tm|r)\)", " ", text or "", flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"\s+-\s+", " ", text))


def detect_cpu(text: str, cpus: dict) -> tuple[str, str] | None:
    """(nivel, texto encontrado) del primer patrón que matchea, recorriendo los niveles en orden."""
    text = normalize_cpu_text(text)
    for tier in CPU_TIERS:
        for c in cpus.get(tier) or []:
            m = re.search(c["re"], text, re.I)
            if m:
                return tier, m.group(0)
    return None


def extract_mpns(text: str) -> list[str]:
    out = []
    for rx in MPN_RES:
        out += [m for m in rx.findall(text or "") if m not in out]
    return out


def product_key(row) -> str:
    """Mismo producto en la misma tienda, aunque llegue por fuentes distintas."""
    mpns = [row["mpn"]] if row["mpn"] else extract_mpns(f"{row['title']} {spec_text(row)}")
    name = mpns[0] if mpns else re.sub(r"\W+", " ", (row["title"] or "").lower())[:70]
    return f"{name}|{(row['store'] or '').lower()}"


def parse_ram_gb(text: str) -> int | None:
    """La primera RAM dicha explícitamente; si no hay, el único tamaño típico de RAM que quede sin contar la VRAM."""
    hits = sorted((m.start(), int(m.group(1))) for rx in RAM_RES for m in rx.finditer(text or ""))
    hits = [gb for _, gb in hits if gb in RAM_SIZES]
    if hits:
        return hits[0]
    sizes = {int(n) for n in re.findall(r"\b(\d{1,3})\s*GB\b", VRAM_RE.sub(" ", text or ""), re.I)} & RAM_SIZES
    return sizes.pop() if len(sizes) == 1 else None


def parse_storage_gb(text: str) -> int | None:
    """El disco más grande mencionado, en GB (1 TB = 1000)."""
    sizes = [float(n.replace(",", ".")) * 1000 for n in re.findall(r"\b(\d+(?:[.,]\d+)?)\s*TB\b", text or "", re.I)]
    sizes += [int(n) for n in re.findall(r"\b(\d{3,4})\s*GB\b", text or "", re.I) if int(n) >= 256]
    return int(max(sizes)) if sizes else None


def parse_delivery(text: str, today: date) -> date | None:
    """Última fecha de entrega estimada: '05/11/2026 - 08/11/2026' -> 8/11, '15 novembre', 'in 2-4 giorni' -> hoy + 4."""
    text = (text or "").lower()
    found = []
    for m in DATE_RE.finditer(text):
        d, mo, y = m.groups()[:3] if m.group(1) else m.groups()[3:]
        found.append((int(d), int(mo), int(y) if y else None))
    for d, m in re.findall(r"\b(\d{1,2})\s+(gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)[a-z]*", text):
        found.append((int(d), MONTHS[m], None))
    dates = []
    for d, m, y in found:
        year = (2000 + y if y < 100 else y) if y else today.year
        try:
            day = date(year, m, d)
            if not y and day < today - timedelta(days=60):
                day = date(year + 1, m, d)  # "05/01" en noviembre es el año que viene
        except ValueError:
            continue  # "24/48 ore" no es una fecha
        dates.append(day)
    if dates:
        return max(dates)
    if "domani" in text:
        return today + timedelta(days=1)
    nums = [int(n) for n in re.findall(r"(\d+)\s*(?:giorni|gg|ore|h)\b", text)]  # en "2-4 giorni", el 4
    if not nums:
        return None
    days = max(nums)
    if re.search(r"\d\s*(?:ore|h)\b", text):
        days = math.ceil(days / 24)
    if "lavorativ" in text:
        days = math.ceil(days * 7 / 5)
    return today + timedelta(days=days)


@dataclass
class Verdict:
    passed: bool
    gpu: str | None
    cpu: str | None = None                        # texto detectado ("8940HX")
    cpu_tier: str | None = None                   # tier1/tier2/tier3/avoid
    reasons: list = field(default_factory=list)   # por qué se descartó
    flags: list = field(default_factory=list)     # señales para el juez
    marks: list = field(default_factory=list)     # marcas visibles en los mensajes (también llegan al juez)
    small_store: bool = False


@dataclass
class BuyNow:
    ok: bool
    failed: list = field(default_factory=list)    # condiciones que no se cumplen
    missing: list = field(default_factory=list)   # datos que faltan para saberlo


def is_marketplace(store, cfg) -> bool:
    s = (store or "").lower()
    return any(m.lower() in s for m in cfg["stores"].get("marketplace_markers", []))


def is_trusted(store, cfg) -> bool:
    s = (store or "").lower()
    return not is_marketplace(store, cfg) and any(t.lower() in s for t in cfg["stores"]["trusted"])


def evaluate(row, cfg) -> Verdict:
    title = row["title"] or ""
    extra = row_extra(row)
    gpu = detect_gpu(title) or detect_gpu(spec_text(row))
    v = Verdict(passed=True, gpu=gpu)
    low = title.lower()

    def reject(reason):
        v.passed = False
        v.reasons.append(reason)

    kw = next((k for k in cfg.get("exclude_keywords", []) if k.lower() in low), None)
    if kw:
        reject(f"palabra excluida: {kw}")

    def in_list(name, values):
        return name is not None and name.lower() in [x.lower() for x in values or []]

    gpus = cfg["gpus"]
    if gpu is None:
        reject("GPU no detectada")
    elif in_list(gpu, gpus.get("fallback")):
        v.flags.append(f"GPU de respaldo (RTX {gpu}): penalizar fuerte")
    elif not in_list(gpu, gpus["preferred"]):
        reject(f"GPU {gpu} no aceptada")
    high_vram = in_list(gpu, gpus["high_vram"])

    cpus = cfg.get("cpus")
    if cpus:
        hit = detect_cpu(title, cpus) or detect_cpu(spec_text(row), cpus)
        if hit is None:
            if cpus.get("unknown", "judge") == "discard":
                reject("CPU no identificado")
            else:
                v.flags.append("CPU no identificado: verificar por MPN")
        else:
            v.cpu_tier, v.cpu = hit
            if v.cpu_tier == "avoid":
                reject(f"CPU descartado ({v.cpu})")
            elif v.cpu_tier == "tier3":
                v.flags.append(f"CPU de nivel 3 ({v.cpu}): penalizar")

    price, pc = row["price"], cfg["price"]
    cap = pc["hard_cap_high_vram"] if high_vram else pc["hard_cap"]
    if price is None:
        reject("sin precio")
    elif price > cap:
        reject(f"precio {price:.0f} € > tope {cap} €")
    else:
        why = suspicious_price(price, gpu, pc)
        if why and pc.get("suspicious_action", "discard") == "flag":
            v.marks.append(f"⚠️ sospechoso: {why}")
        elif why:
            reject(f"precio sospechoso ({why})")
        if price > pc["target"]:
            v.flags.append(f"sobre el tope objetivo ({pc['target']} €)")

    available = row["available"]
    if available is not None and not available:
        reject("no disponible")

    store = row["store"] or ""
    if any(b.lower() in store.lower() for b in cfg["stores"]["blocked"]):
        reject(f"tienda bloqueada: {store}")
    elif not is_trusted(store, cfg):
        v.small_store = True
        v.flags.append("marketplace (vendedor tercero)" if is_marketplace(store, cfg) else "tienda chica")
        small = cfg["stores"].get("small", {})
        payments = [p.lower() for p in extra.get("payments") or []]
        rating, reviews = extra.get("store_rating"), extra.get("store_reviews")
        if payments and small.get("require_card", True) and not any(p in ("visa", "mastercard") for p in payments):
            reject("tienda chica sin Visa/Mastercard")
        if rating is not None and rating < small.get("min_rating", 0):
            reject(f"tienda chica con rating {rating}")
        if reviews is not None and reviews < small.get("min_reviews", 0):
            reject(f"tienda chica con {reviews} opiniones")
        if not payments:
            v.flags.append("medios de pago desconocidos")
        if rating is None:
            v.flags.append("rating de tienda desconocido")

    if row["source"] == "idealo":
        v.flags.append("precio de idealo (oferta más barata; verificar tienda)")
    if extra.get("stock_unverified"):
        v.flags.append("stock sin verificar en la ficha (el índice de Unieuro puede estar desactualizado)")
    if extra.get("charger_separate"):
        v.flags.append("cargador vendido por separado")
    if "slim" in low or extra.get("slim"):
        v.flags.append("chasis slim: riesgo de TGP bajo")
    if high_vram:
        v.flags.append("más de 8 GB de VRAM")
    # no descartan: marcan (en Unieuro, "OUTLET ONLINE" es un badge de la ficha, no parte del título)
    haystack = " ".join([title, spec_text(row), *(extra.get("badges") or []), extra.get("promo") or ""]).lower()
    v.marks += [f"🏷️ {k}" for k in cfg.get("flag_keywords") or [] if k.lower() in haystack]
    return v


def suspicious_price(price, gpu, pc) -> str | None:
    """Motivo si el precio es sospechosamente bajo: por debajo del umbral de esa GPU o de min_sane."""
    floors = {str(k).lower(): v for k, v in (pc.get("suspicious_below") or {}).items()}
    floor = floors.get((gpu or "").lower())
    if floor is not None and price < floor:
        return f"{gpu} a {price:.0f} €, umbral {floor:.0f} €"
    if price < pc["min_sane"]:
        return f"{price:.0f} €, mínimo {pc['min_sane']:.0f} €"
    return None


def check_buy_now(row, v, cfg) -> BuyNow | None:
    """Regla "🟢 COMPRAR YA" de criteria.yaml (None si no hay). Con un dato faltante no se marca."""
    rule = cfg.get("buy_now")
    if not rule:
        return None
    failed, missing = [], []
    text = f"{row['title']} {spec_text(row)}"
    if not v.gpu or v.gpu.lower() not in [g.lower() for g in rule["gpus"]]:
        failed.append(f"GPU {v.gpu or 'no detectada'}")
    if v.cpu_tier is None:
        missing.append("CPU")
    elif v.cpu_tier not in rule["cpu_tiers"]:
        failed.append(f"CPU de nivel {v.cpu_tier[-1]}")
    ram, storage = parse_ram_gb(text), parse_storage_gb(text)
    if ram is None:
        missing.append("RAM")
    elif ram < rule["min_ram_gb"]:
        failed.append(f"RAM {ram} GB < {rule['min_ram_gb']} GB")
    if storage is None:
        missing.append("almacenamiento")
    elif storage < rule["min_storage_gb"]:
        failed.append(f"almacenamiento {storage} GB < {rule['min_storage_gb']} GB")
    if row["price"] is None or row["price"] > rule["max_price"]:
        failed.append(f"precio > {rule['max_price']} €")
    if rule.get("trusted_store_only", True):
        if row["source"] == "idealo":
            missing.append("vendedor (idealo no confirma la tienda)")
        elif not is_trusted(row["store"], cfg):
            failed.append("tienda no confiable o marketplace")
    if rule.get("must_deliver_by", True):
        extra = row_extra(row)
        eta = parse_delivery(extra.get("delivery") or extra.get("availability"), today_rome())
        if eta is None:
            missing.append("fecha de entrega")
        elif eta > cfg.dates["deliver_by"]:
            failed.append(f"entrega {eta:%d/%m} después del {cfg.dates['deliver_by']:%d/%m}")
    return BuyNow(ok=not failed and not missing, failed=failed, missing=missing)


def all_marks(v, bn) -> list:
    return (["🟢 COMPRAR YA"] if bn and bn.ok else []) + v.marks
