"""Filtro determinístico previo al juez: GPU, CPU, precio, disponibilidad, tienda, palabras excluidas."""
import json
import re
from dataclasses import dataclass, field

GPU_RE = re.compile(r"RTX\W{0,3}(\d{4})(\s?Ti\b)?", re.I)
CPU_TIERS = ("tier1", "tier2", "tier3", "avoid")
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


@dataclass
class Verdict:
    passed: bool
    gpu: str | None
    cpu: str | None = None                        # texto detectado ("8940HX")
    cpu_tier: str | None = None                   # tier1/tier2/tier3/avoid
    reasons: list = field(default_factory=list)   # por qué se descartó
    flags: list = field(default_factory=list)     # señales para el juez
    small_store: bool = False


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
    elif price < pc["min_sane"]:
        reject(f"precio sospechoso ({price:.0f} €)")
    elif price > cap:
        reject(f"precio {price:.0f} € > tope {cap} €")
    elif price > pc["target"]:
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
    if extra.get("charger_separate"):
        v.flags.append("cargador vendido por separado")
    if "slim" in low or extra.get("slim"):
        v.flags.append("chasis slim: riesgo de TGP bajo")
    if high_vram:
        v.flags.append("más de 8 GB de VRAM")
    return v
