"""Scrape homegate.ch, immoscout24.ch and newhome.ch rental searches via a real, visible browser.

The sites sit behind DataDome / Cloudflare, so we drive a headed Chrome with a persistent
profile: when a captcha shows up you solve it by hand in the window, and the cookies stick
for later runs.

    python scrape.py            # scrape all sites using data/state.json, write data/*.json
"""
import hashlib
import json
import math
import os
import random
import re
import shutil
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ENV_FILE = Path(__file__).parent / "deploy" / ".env"


def load_env(path=ENV_FILE):
    """Read KEY=value lines (# comments, optional quotes, optional `export `) into os.environ.
    Variables already set in the real environment win over the file."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env()  # every entry point (app, scrape, triage, commute, calibrate) imports this module

DATA = Path(__file__).parent / "data"
LISTINGS = DATA / "listings.json"  # every listing ever seen, keyed "site:id"
STATE = DATA / "state.json"  # search params, blocklist, hidden ids (edited via the UI)
ELIGIBLE = DATA / "eligible.json"  # filtered output for post-processing
PROFILE = DATA / "browser-profile"

DEFAULT_STATE = {
    "search": {
        "sites": ["homegate", "immoscout24", "newhome", "flatfox"],
        "move_in_earliest": "", "move_in_latest": "",  # YYYY-MM-DD, "" = no limit
        "locations": [{"id": "geo-city-zurich", "label": "Zürich (Ort)", "homegate": "city-zurich",
                       "immoscout24": "city-zurich", "newhome": "ort-zuerich"}],
        "min_rooms": 3,
        "min_area": 60,
        "max_rent": 0,  # 0 = no limit
        "max_pages": 10,
    },
    "rev": 0,  # bumped on every save; a save based on an older rev is rejected (stale tab)
    "blocklist": [],
    "hidden": [],
    # areas drawn on the map whose listings are excluded: {id, name, shape: "rectangle"|"polygon", on, points: [[lat, lon]]}
    "exclusion_areas": [],
    "provider_labels": {},  # logo hash -> name, for sites that only show a provider logo
    "commute": {  # commute.py; limits 0/None/"" = no limit
        # destinations: {name, addresses (one per line, best counts), mode, arrive_by, max_minutes, max_km,
        #                max_transfers, free_train_transfers, return_at (night return departure), max_night_minutes}
        "points": [], "auto": False,
    },
    "triage": {  # AI triage (triage.py); provider "both" = flag only what both models agree on; model "" = default
        "provider": "anthropic", "models": {"anthropic": "", "openai": ""}, "auto": False, "max_images": 8,
        "custom_rules": "",  # user's exclusion criteria, one unwanted kind of listing per line
        # which AI findings exclude a listing; kitchen: exclude this size and smaller ("" = off);
        # temporary_min_until: keep temporary leases that run at least until this YYYY-MM-DD ("" = exclude all)
        "exclude": {"temporary": True, "temporary_min_until": "", "furnished": True, "swap": True,
                    "shared": True, "kitchen": "tiny", "custom": True},
    },
    # swisstaxmap.ch inputs; income 0 disables the tax columns
    "tax": {"income": 0, "age": 30, "married": False, "income2": 0, "age2": 30, "children": []},
}
CAPTCHA_TIMEOUT = 300
TAXES = DATA / "taxes.json"  # cache: [profile, zip] -> {tax, commune}
FLATFOX = DATA / "flatfox.json"  # flatfox listing details by pk (fetched once)
AREAS = DATA / "areas.json"  # location id -> [west, south, east, north] (swisstopo)
ZIPS = DATA / "zips.json"  # zip -> ids of the places it belongs to (homegate geo API)
LOGOS = DATA / "logos.json"  # cache: logo url -> content hash
COMMUTE = DATA / "commute.json"  # route cache: commute_key() -> [{min, km, rides: [{type, line}], wait_min?}]
TRIAGE = DATA / "triage.json"  # AI triage results: "site:id" -> {v, result}
NOTES = DATA / "notes.json"  # your own triage: "site:id" -> {status, starred, tags, note, pinned, seen, updated}
NOTES_BACKUPS = DATA / "backups" / "notes"  # notes.json as it was before each edit, newest NOTES_KEEP kept
NOTES_KEEP = 500
PHOTOS = DATA / "photos.json"  # photo url -> perceptual hash (dedup.py), to recognise one flat on several portals


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save(path, obj):
    """Write via a temp file and rename, so the UI's threads never read a half-written file."""
    DATA.mkdir(exist_ok=True)
    tmp = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    tmp.replace(path)


def load_state():
    """Saved state on top of the defaults (so new settings appear for old state files)."""
    state = load(STATE, {})
    merged = {**DEFAULT_STATE, **state}
    for k in ("search", "tax", "triage", "commute"):
        merged[k] = {**DEFAULT_STATE[k], **state.get(k, {})}
    for k in ("exclude", "models"):
        merged["triage"][k] = {**DEFAULT_STATE["triage"][k], **merged["triage"][k]}
    return merged


# ---------- taxes ----------

def tax_for(p, zip_):
    """Yearly federal + cantonal + communal (+ personal) income tax at a ZIP, via swisstaxmap.ch."""
    married = p["married"]
    body = {"TaxLocationID": f"{zip_}00000", "Revenue1": p["income"], "Age1": p["age"], "Age2": p["age2"],
            "Relationship": 2 if married else 1, "Revenue2": p["income2"] if married else 0,
            "RevenueType2": 1 if married else 0, "Children": [{"Age": a} for a in p["children"]]}
    req = urllib.request.Request("https://www.swisstaxmap.ch/calculate-tax", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.load(r)["response"]
        # A ZIP spanning several communes gets whichever commune swisstaxmap picks.
        return {"tax": res["TotalTax"], "commune": res["Location"]["BfsName"]}
    except Exception:
        return None  # unknown ZIP / API down: no tax column for this listing, retried next time


# Lookups that just failed (unknown ZIP, logo that won't download): retried only after a while, because every
# /api/data rebuild would retry them, and saving an unchanged cache file would make the UI's next request rebuild too.
RETRY_AFTER = 600
_failed = {}  # key -> time of the last failed lookup


def fetch_missing(cache, keys, fetch, path):
    """Fetch the keys not in cache (except recent failures) 8 at a time; save the cache only if something was added."""
    now = time.time()
    if not (todo := [k for k in keys if now - _failed.get(k, 0) > RETRY_AFTER]):
        return
    with ThreadPoolExecutor(8) as ex:
        found = dict(zip(todo, ex.map(fetch, todo)))
    _failed.update({k: now for k, v in found.items() if not v})
    if new := {k: v for k, v in found.items() if v}:
        cache |= new
        save(path, cache)


def add_taxes(listings, profile):
    cache = load(TAXES, {})
    key = lambda z: json.dumps([profile, z], sort_keys=True)
    missing = sorted({l["zip"] for l in listings.values() if l["zip"] and key(l["zip"]) not in cache})
    if missing and profile["income"]:
        fetch_missing(cache, [key(z) for z in missing], lambda k: tax_for(profile, json.loads(k)[1]), TAXES)
    for l in listings.values():
        t = cache.get(key(l["zip"])) or {}
        l["tax_year_chf"], l["tax_commune"] = t.get("tax"), t.get("commune")
    # Baseline: cheapest-tax commune among the current search's listings (stable when hiding/blocking).
    current = [l["tax_year_chf"] for l in listings.values() if l["tax_year_chf"] is not None
               and l.get("excluded") not in ("deleted", "other search", "site not selected")]
    base = min(current, default=None)
    for l in listings.values():
        known = l["tax_year_chf"] is not None and base is not None
        l["tax_extra_month_chf"] = round((l["tax_year_chf"] - base) / 12) if known else None
        l["rent_tax_adjusted_chf"] = l["rent_chf"] + l["tax_extra_month_chf"] if known and l["rent_chf"] else None


# ---------- locations ----------

def location(g):
    """Map a homegate geo-API location to each site's URL slug."""
    kind = g["id"].split("-")[1]  # city, region, canton, zipcode, cityregion
    return {
        "id": g["id"], "label": f"{' '.join(g['names']['de'])} ({g['typeLabel']['de']})",
        "homegate": g["urlNames"]["en"], "immoscout24": g["is24UrlNames"]["en"],
        # newhome: districts use the English slug (region-horgen), towns/cantons German (ort-/kanton-); no ZIP pages
        "newhome": None if kind == "zipcode" else g["urlNames"]["en" if kind == "region" else "de"],
    }


def find_locations(q):
    """Location autocomplete. The homegate geo API, unlike the site, isn't bot-protected.

    Accepts "Meilen", "Bezirk Meilen", "Meilen (Bezirk)", "Kanton Zürich", "8706"; a type word ranks that type first.
    """
    types = {"bezirk": "bezirk", "region": "bezirk", "district": "bezirk", "kanton": "kanton", "canton": "kanton",
             "ort": "ort", "city": "ort", "plz": "plz", "zip": "plz"}
    name, kind = q.strip(), ""
    if m := re.fullmatch(r"(.+?)\s*\((\w+)\)", name):  # "Meilen (Bezirk)"
        name, kind = m[1], m[2]
    elif (m := re.fullmatch(r"(\w+)\s+(.+)", name)) and m[1].lower() in types:  # "Bezirk Meilen"
        kind, name = m[1], m[2]
    kind = types.get(kind.lower(), "")
    url = "https://api.homegate.ch/geo/locations?" + urllib.parse.urlencode({"lang": "de", "size": 10, "name": name})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as r:
        locs = [location(x["geoLocation"]) for x in json.load(r)["results"]]
    return sorted(locs, key=lambda l: bool(kind) and f"({kind})" not in l["label"].lower())


# ---------- extraction ----------

def is_listing(x):
    d = x.get("listing", x)
    return isinstance(d, dict) and ("immocode" in d or ("id" in d and ("prices" in d or "characteristics" in d)))


def find_listings(obj):
    """Largest array of listing-like objects anywhere in a JSON blob."""
    best = []

    def walk(o):
        nonlocal best
        if isinstance(o, list):
            items = [x for x in o if isinstance(x, dict) and is_listing(x)]
            if len(items) > len(best):
                best = items
            o = [x for x in o if not (isinstance(x, dict) and is_listing(x))]
        for v in o.values() if isinstance(o, dict) else o if isinstance(o, list) else []:
            walk(v)

    walk(obj)
    return best


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize(site, x):
    """A site's raw listing JSON as our listing fields."""
    l = _normalize(site, x)
    return l | listing_floor(site, l)


def _normalize(site, x):
    if site == "flatfox":
        abs_url = lambda u: f"https://flatfox.ch{u}" if u and u.startswith("/") else u
        agency, avail = x.get("agency") or {}, x.get("moving_date_type")
        rent = x.get("rent_gross") or (x.get("rent_net") and x["rent_net"] + (x.get("rent_charges") or 0)) or x.get("price_display")
        return {
            "site": site, "id": str(x["pk"]), "title": x.get("public_title") or x.get("short_title"),
            "url": abs_url(x.get("url")) or f"https://flatfox.ch/en/flat/{x['pk']}/",
            "rooms": num(x.get("number_of_rooms")), "area_m2": num(x.get("livingspace") or x.get("surface_living")),
            "rent_chf": num(rent), "address": x.get("street"), "zip": str(x["zipcode"]) if x.get("zipcode") else None,
            "city": x.get("city"), "lat": x.get("latitude"), "lon": x.get("longitude"),
            "available_from": {"imm": "immediately", "agr": "by arrangement"}.get(avail) or (x.get("moving_date") or "")[:10] or None,
            "published": x.get("published"),
            "provider": agency.get("name"),
            "provider_logo": abs_url(agency["logo"] if isinstance(agency.get("logo"), str) else (agency.get("logo") or {}).get("url")),
            "description": x.get("description"),
            "images": [abs_url(i["url"]) for i in x.get("images") or [] if i.get("url")],
            "smg_id": x.get("smg_id") or None, "source": "search", "raw": x,
        }
    if site == "newhome":
        lid = str(x["immocode"])
        city = (x.get("city") or "").lower().translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", " ": "-"}))
        rooms = f"{x.get('rooms') or 1:g}"
        return {
            "site": site, "id": lid, "title": x.get("title"),
            "url": f"https://www.newhome.ch/de/mieten/immobilien/wohnung/wohnung/ort-{city}/{rooms}-zimmer/detail/{lid}",
            "rooms": num(x.get("rooms")), "area_m2": num(x.get("livingArea")),
            "rent_chf": num(x.get("price")), "address": x.get("street"),
            "zip": x.get("postalCode"), "city": x.get("city"),
            "available_from": {1: "immediately", 3: "by arrangement"}.get(x.get("availabilityType"))
                              or (x.get("availabilityDate") or "")[:10] or None, "published": None,
            "provider": x.get("contactCompanyName"), "provider_logo": x.get("contactLogoUrl"),
            "lat": x.get("latitude"), "lon": x.get("longitude"),
            "description": (x.get("detail") or {}).get("descriptionTextPlain"),
            # images carry AI captions ("Küche - ..."); kitchen shots first so they survive the triage image cap
            # uncropped originals ("-o") instead of the cropped search thumbnails ("-trnh")
            "images": [re.sub(r"-trnh\.jpg$", "-o.jpg", i["path"]) for i in sorted(
                x.get("images") or [], key=lambda i: not (i.get("labels") or "").lower().startswith("küche"))],
            "source": "search", "raw": x,
        }
    # The listing page is the source of truth: search results can lag edits or show another unit of a project.
    d = (x.get("detail") or {}).get("listing") or x.get("listing", x)
    lid = str(d["id"])
    c, a = d.get("characteristics") or {}, d.get("address") or {}
    texts = [v.get("text") or {} for v in (d.get("localization") or {}).values() if isinstance(v, dict)]
    brand, lister = x.get("listerBranding") or {}, d.get("lister") or {}
    return {
        "site": site, "id": lid, "title": next((t.get("title") for t in texts if t.get("title")), None),
        "url": f"https://www.{site}.ch/rent/{lid}",
        "rooms": num(c.get("numberOfRooms")), "area_m2": num(c.get("livingSpace")),
        "rent_chf": num(((d.get("prices") or {}).get("rent") or {}).get("gross")),
        "address": a.get("street"), "zip": a.get("postalCode"), "city": a.get("locality") or a.get("city"),
        "available_from": (x.get("detail") or {}).get("available") or d.get("availableFrom"),
        "published": (d.get("meta") or {}).get("createdAt"),
        # SMG search results carry no provider name, only a logo (see add_providers)
        "provider": brand.get("legalName") or brand.get("name") or lister.get("name"),
        "provider_logo": brand.get("logoUrl") or lister.get("logoUrl"),
        "lat": (a.get("geoCoordinates") or {}).get("latitude"), "lon": (a.get("geoCoordinates") or {}).get("longitude"),
        "description": next((t.get("description") for t in texts if t.get("description")), None),
        "images": list(dict.fromkeys(  # 800px CDN rendition instead of the original
            re.sub(r"^(https://[^/]+)/", r"\1/t_web_dp_small/", a["url"])
            for v in (d.get("localization") or {}).values() if isinstance(v, dict)
            for a in v.get("attachments") or [] if a.get("type") == "IMAGE" and a.get("url"))),
        "source": "page" if (x.get("detail") or {}).get("listing") else "search",
        "raw": x,
    }


# ---------- floor ----------

# newhome's FloorType enum (from its web app): 0 none, 1 basement, 2 ground floor, 3-10 = 1st-8th, 21-32 = 9th-20th
NEWHOME_FLOORS = {1: -1, 2: 0, **{v: v - 2 for v in range(3, 11)}, **{v: v - 12 for v in range(21, 33)},
                  11: "DG", 13: "Hochparterre", 14: "mehrstöckig", 33: "über 20. Stock"}
FLOOR_RE = re.compile(
    r"(?P<n>\d{1,2})\s*(?:\.|st|nd|rd|th|e|er|ème|eme|°)?\s*(?:OG|Obergeschoss|Stock(?:werk)?|Etage|étage|floor|piano)\b"
    r"|(?P<hp>Hochparterre|raised ground floor|rez surélevé)"
    # a basement in a listing is nearly always the cellar or laundry: only explicit basement flats count
    r"|(?P<ug>(?<=wohnung im )(?:Untergeschoss|UG|Souterrain)|Souterrain\w*wohnung|Tiefparterre|lower ground floor"
    r"|basement (?:flat|apartment)|(?<=apartment in the )basement)"
    r"|(?P<eg>Erdgeschoss|Parterre|\bEG\b|ground floor|rez-de-chauss[ée]e|piano terra)"
    r"|(?P<attika>Attika)|(?P<dg>Dachgeschoss|\bDG\b|Dachwohnung|Mansarde|attic)", re.I)
# a floor counts when the flat is named in the same sentence ("3.5-Zimmer-Wohnung im 2. OG", "im 4. OG liegt die Wohnung")
_FLAT = r"\b(?:\w*wohnung|apartment|appartement|flat|logement|appartamento|zimmer|rooms?|pièces|locali)\b"
FLAT_BEFORE = re.compile(_FLAT + r"[^.!?:;\n|*]{0,50}$", re.I)
FLAT_AFTER = re.compile(r"^[^.!?:;\n|*]{0,40}" + _FLAT, re.I)
# ...unless it's another flat (photos of "einer identischen Wohnung im 1. OG") or the cellar ("Kellerabteil pro Wohnung im UG")
OTHER_FLAT = re.compile(r"(?:ähnlich|identisch|similar|identical|gleich|keller|abteil)\w*\W+(?:\w+\W+){0,4}$", re.I)
FLOOR_ORDER = {"UG": -1, "EG": 0, "Hochparterre": 0.5, "über 20. Stock": 21, "DG": 50, "Attika": 50}


def floor_label(f):
    """A floor number (0 ground floor, negative basement) in German convention: EG, UG, 2. UG, 3. Stock."""
    return "EG" if f == 0 else "UG" if f == -1 else f"{-f}. UG" if f < 0 else f"{f}. Stock"


def floor_from_text(*texts):
    """The flat's floor as the listing's title or description states it, or None."""
    for text in texts:
        text = re.sub(r"<[^>]+>", " ", text or "")
        for m in FLOOR_RE.finditer(text):
            before = text[max(0, m.start() - 60):m.start()]
            if not (FLAT_BEFORE.search(before) or FLAT_AFTER.search(text[m.end():m.end() + 50])) or OTHER_FLAT.search(before):
                continue
            if m["n"]:
                if 1 <= int(m["n"]) <= 40:
                    return f"{int(m['n'])}. Stock"
                continue
            return "Hochparterre" if m["hp"] else "UG" if m["ug"] else "EG" if m["eg"] else "Attika" if m["attika"] else "DG"
    return None


def listing_floor(site, l):
    """floor: the portal's floor field, else the description's (floor_source "text"); floor_text: the description's,
    to flag a contradiction (see conflicts). floor_n sorts them. Worked out from the stored raw data, so no re-scrape."""
    x = l["raw"]
    if site == "flatfox":
        f = x.get("floor")
    elif site == "newhome":
        f = NEWHOME_FLOORS.get(x.get("floor"))
    else:
        c = ((x.get("detail") or {}).get("listing") or x.get("listing", x)).get("characteristics") or {}
        f = c.get("floor")
        if c.get("isGroundFloorRaised") and f in (0, None):
            f = "Hochparterre"
        elif f is None and c.get("isGroundFloor"):
            f = 0
    if isinstance(f, (int, float)):
        # "4. UG" / "99. Stock" are typos on the portal (the text then says "4. OG"), not a floor
        f = floor_label(int(f)) if -1 <= f <= 40 else None
    text = floor_from_text(l["title"], l["description"])
    floor = f or text
    n = re.match(r"(\d+)\. (Stock|UG)", floor or "")
    return {"floor": floor, "floor_source": "text" if floor and not f else None, "floor_text": text,
            "floor_n": (int(n[1]) * (1 if n[2] == "Stock" else -1)) if n else FLOOR_ORDER.get(floor)}


KEEP = ("first_seen", "last_seen", "found_in", "deleted_at")  # bookkeeping kept across re-normalization


_listings_cache = (None, {})  # (listings.json signature, search locations), normalized listings


def load_listings(cached=True):
    """Stored listings, re-normalized from their raw site JSON so field changes apply to old data too.
    Parsing and normalizing takes seconds with a few thousand listings, so the result is kept until
    listings.json or the searched locations change; each call gets its own copy of every listing.
    cached=False: a private copy to change and save (a scrape edits the raw data in place)."""
    global _listings_cache
    locations = [x["id"] for x in load_state()["search"]["locations"]]
    if not cached:
        return _load_listings(locations)
    try:
        st = LISTINGS.stat()
        sig = (st.st_mtime_ns, st.st_size, tuple(locations))
    except FileNotFoundError:
        sig = None
    if sig is None or _listings_cache[0] != sig:
        _listings_cache = (sig, _load_listings(locations))
    return {k: dict(l) for k, l in _listings_cache[1].items()}


def _load_listings(locations):
    stored = load(LISTINGS, {})
    latest = {}
    for l in stored.values():
        latest[l["site"]] = max(latest.get(l["site"], ""), l["last_seen"])
    out = {}
    for k, l in stored.items():
        extra = {f: l[f] for f in KEEP if f in l}
        # listings from before searches were tracked: the latest scrape used today's locations
        extra.setdefault("found_in", locations if l["last_seen"] == latest[l["site"]] else [])
        out[k] = {**normalize(l["site"], l["raw"]), **extra}
    return out


# ---------- filtering ----------

def logo_hash(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as r:
            return hashlib.sha1(r.read()).hexdigest()[:12]
    except Exception:
        return None  # retried next time


def add_providers(listings, labels):
    """Identify providers by logo content: homegate and immoscout24 serve byte-identical logos."""
    cache = load(LOGOS, {})
    if missing := sorted({l["provider_logo"] for l in listings.values() if l["provider_logo"]} - cache.keys()):
        fetch_missing(cache, missing, logo_hash, LOGOS)
    for l in listings.values():
        l["provider_key"] = cache.get(l["provider_logo"])
        l["provider_label"] = labels.get(l["provider_key"]) or l["provider"]


_raw_text = {}  # id(raw) -> (raw, its lowercased JSON): the slow part of blocked_by, same until the raw data changes


def raw_text(raw):
    hit = _raw_text.get(id(raw))
    if not hit or hit[0] is not raw:
        if len(_raw_text) > 20000:  # listings.json was re-read many times: drop entries of old copies
            _raw_text.clear()
        hit = _raw_text[id(raw)] = (raw, json.dumps(raw, ensure_ascii=False).lower())
    return hit[1]


# Trailing words dropped from a blocklist term, so "Apleona Schweiz AG" also blocks "Apleona" and "Apleona Real Estate AG"
COMPANY_SUFFIXES = {"ag", "sa", "gmbh", "sàrl", "sarl", "ltd", "inc", "kg", "co", "&", "schweiz", "suisse", "svizzera",
                    "switzerland", "swiss", "ch", "real", "estate", "immobilien", "immobilier", "management",
                    "services", "group", "holding"}


def block_core(term):
    """A blocklist term without its company-form/country tail; the whole term when that leaves too little."""
    words = term.strip().lower().rstrip(".").split()
    while len(words) > 1 and words[-1].strip(".,") in COMPANY_SUFFIXES:
        words.pop()
    core = " ".join(words).strip(".,")
    return core if len(core) >= 3 else term.strip().lower()


def blocked_by(listing, blocklist):
    """First blocklist term found (as itself or its core name, see block_core) in the provider label or anywhere in
    the listing (name, logo URL, description...)."""
    if not any(b.strip() for b in blocklist):
        return None
    text = raw_text(listing["raw"]) + " " + (listing["provider_label"] or "").lower()
    return next((b for b in blocklist if b.strip() and (
        b.strip().lower() in text or re.search(r"(?<!\w)" + re.escape(block_core(b)) + r"(?!\w)", text))), None)


AREA_RE = re.compile(r"(\d{2,3})\s*m(?:²|2)\s*Wohnfl|Wohnfläche[^0-9<]{0,20}(\d{2,3})\s*m|"
                     r"(?:auf|ca\.|rund|approx(?:imately)?\.?|about|environ)\s+(\d{2,3})\s*m(?:²|2)", re.I)
ROOMS_RE = re.compile(r"(\d+(?:[.,]5)?|\d\s*½)\s*[- ]?\s*(?:Zimmer|Zi\b|room|pièces|pieces|locali)", re.I)


def conflicts(l, twin):
    """Fields whose value is contradicted by the listing's own text or by the other portal: {field: reason}.
    Values verified on the listing page are trusted, except the floor: the portal's field is often a typo."""
    out = {}
    # an attic is also a numbered floor, and a Hochparterre is counted as EG or 1. Stock
    floor, text = l.get("floor"), l.get("floor_text")
    if floor and l.get("floor_source") is None and text and text != floor and not (
            text in ("DG", "Attika") or "Hochparterre" in (floor, text) and {floor, text} <= {"EG", "Hochparterre", "1. Stock"}):
        out["floor"] = f"description says {text}"
    if l["source"] == "page":
        return out
    if l["rooms"] and (m := ROOMS_RE.search(l["title"] or "")):
        n = float(m[1].replace(",", ".").replace(" ", "").replace("½", ".5"))
        if n and n != l["rooms"]:
            out["rooms"] = f"title says {m[1]} rooms"
    text = re.sub(r"<[^>]+>", " ", l["description"] or "")
    areas = {int(g) for t in AREA_RE.findall(text) for g in t if g}
    if l["area_m2"] and areas and all(abs(a - l["area_m2"]) > 0.1 * l["area_m2"] for a in areas):
        out["area_m2"] = f"description says {', '.join(map(str, sorted(areas)))} m²"
    if twin and twin["source"] != "page":
        for f, unit in (("rooms", " rooms"), ("area_m2", " m²"), ("rent_chf", " CHF")):
            if l[f] is not None and twin[f] is not None and l[f] != twin[f]:
                out.setdefault(f, f"{twin['site']} says {twin[f]:g}{unit}")
    return out


def fails_search(l, s):
    """Search limits; a field with conflicting data (see conflicts()) never excludes."""
    bad = l.get("conflicts") or {}
    if s["min_rooms"] and l["rooms"] is not None and "rooms" not in bad and l["rooms"] < s["min_rooms"]:
        return f"rooms<{s['min_rooms']}"
    if s["min_area"] and l["area_m2"] is not None and "area_m2" not in bad and l["area_m2"] < s["min_area"]:
        return f"area<{s['min_area']}"
    if s["max_rent"] and l["rent_chf"] is not None and "rent_chf" not in bad and l["rent_chf"] > s["max_rent"]:
        return f"rent>{s['max_rent']}"
    # "by arrangement" and unknown dates never exclude; "immediately" means today
    if (a := l["available_from"]) and a != "by arrangement":
        d = date.today().isoformat() if a == "immediately" else a
        if s["move_in_earliest"] and d < s["move_in_earliest"] or s["move_in_latest"] and d > s["move_in_latest"]:
            return f"available {a}"
    return None


TRAINS = {"RAIL", "HEAVY_RAIL", "COMMUTER_TRAIN", "HIGH_SPEED_TRAIN", "LONG_DISTANCE_TRAIN", "METRO_RAIL", "SUBWAY", "MONORAIL"}


def addresses(p):
    """A destination's alternative addresses (one per line): the best route to any of them counts."""
    return [a.strip() for a in (p.get("addresses") or p.get("address") or "").splitlines() if a.strip()]


def commute_key(l, p, addr, back=False):
    when = (p["return_at"] if back else p["arrive_by"]) if p["mode"] == "transit" else ""
    return f"v2|{'back' if back else 'to'}|{l['lat']:.4f},{l['lon']:.4f}|{p['mode']}|{addr}|{when}"


def transfers(route, free_train):
    """Changes between rides; train-to-train changes are free if the destination says so."""
    rides = route["rides"]
    return sum(not (free_train and a["type"] in TRAINS and b["type"] in TRAINS) for a, b in zip(rides, rides[1:]))


def best_route(alts_by_addr, p):
    """Fastest route to any address within the transfer limit (else the fastest overall); None if no route."""
    cands = [r | {"to": a, "transfers": transfers(r, p.get("free_train_transfers"))}
             for a, alts in alts_by_addr.items() for r in alts]
    limit = p.get("max_transfers")
    ok = [r for r in cands if p["mode"] != "transit" or limit in (None, "") or r["transfers"] <= int(limit)]
    return min(ok or cands, key=lambda r: r["min"], default=None)


def commute_result(l, p, routes):
    """Best route (+ night return), "no route", or None while not (fully) computed."""
    if l["lat"] is None or not addresses(p):
        return None
    outs = {a: routes.get(commute_key(l, p, a)) for a in addresses(p)}
    if any(v is None for v in outs.values()):
        return None
    if not (best := best_route(outs, p)):
        return "no route"
    if p["mode"] == "transit" and p.get("return_at"):
        backs = [routes.get(commute_key(l, p, a, back=True)) for a in addresses(p)]
        waits = [r["wait_min"] for b in backs if b for r in b]
        best["night_min"] = min(waits) if all(b is not None for b in backs) and waits else None
    return best


def commute_reasons(l, points):
    """Limit violations; missing data ("no route", not computed, no night data) never excludes."""
    reasons = []
    for p, r in zip(points, l["commute"]):
        if not isinstance(r, dict):
            continue
        if p["max_minutes"] and r["min"] > p["max_minutes"]:
            reasons.append(f"{p['name']} {r['min']} min")
        if p["max_km"] and r["km"] > p["max_km"]:
            reasons.append(f"{p['name']} {r['km']} km")
        if p["mode"] == "transit" and p.get("max_transfers") not in (None, "") and r["transfers"] > int(p["max_transfers"]):
            reasons.append(f"{p['name']} {r['transfers']} transfers")
        if p.get("max_night_minutes") and r.get("night_min") is not None and r["night_min"] > p["max_night_minutes"]:
            reasons.append(f"{p['name']} night return {r['night_min']} min")
    return reasons


KITCHEN_SIZES = ["tiny", "small", "normal", "large"]


FLAGS = ("temporary", "furnished", "swap", "shared")
AI_PROMPT_VERSION = 12  # bump when triage.SYSTEM or SCHEMA change: older results are ignored and re-triaged


def ai_providers(cfg):
    return ["anthropic", "openai"] if cfg["provider"] == "both" else [cfg["provider"]]


def flagged(r, f):
    """A flag only counts with its own evidence quote."""
    return bool(r[f] and r["evidence"].get(f, "").strip())


def matched_exclusions(r):
    """The user's own exclusion criteria this listing matches (with evidence)."""
    return list(dict.fromkeys(x["criterion"] for x in r["exclusions"] if x["matches"] and x["evidence"].strip()))


def consensus(a, b):
    """Merge two models' triage results, keeping only findings both agree on."""
    sizes = KITCHEN_SIZES + ["unknown"]  # unknown = no exclusion, like "large"
    r = {f: flagged(a, f) and flagged(b, f) for f in FLAGS}
    r["temporary_until"] = max([d for d in (a["temporary_until"], b["temporary_until"]) if d], default=None)
    r["kitchen"] = sizes[max(sizes.index(a["kitchen"]), sizes.index(b["kitchen"]))]  # the less damning size
    # match criteria by text: models may repeat a criterion (one entry per piece of evidence)
    ma, mb = set(matched_exclusions(a)), set(matched_exclusions(b))
    r["exclusions"] = [{"criterion": c, "matches": c in ma and c in mb,
                        "evidence": " | ".join(x["evidence"] for x in a["exclusions"] + b["exclusions"]
                                               if x["criterion"] == c and x["evidence"])}
                       for c in dict.fromkeys(x["criterion"] for x in a["exclusions"] + b["exclusions"])]
    r["evidence"] = {f: " | ".join(f"{n}: {x['evidence'][f]}" for n, x in (("Claude", a), ("GPT", b)) if x["evidence"][f])
                     for f in [*FLAGS, "kitchen"]}
    r["disputed"] = [f for f in FLAGS if flagged(a, f) != flagged(b, f)] + [
        c for c in dict.fromkeys(x["criterion"] for x in a["exclusions"] + b["exclusions"]) if (c in ma) != (c in mb)] + (
        [f"kitchen {a['kitchen']} vs {b['kitchen']}"] if a["kitchen"] != b["kitchen"] else [])
    r["summary"] = a["summary"]
    r["available_from"] = a["available_from"] or b["available_from"]  # Claude's reading wins if they differ
    r["kitchen_by"] = {"Claude": a["kitchen"], "GPT": b["kitchen"]}
    return r


def content_hash(l):
    """What the AI saw: a changed listing (e.g. refreshed from its page) is triaged again."""
    return hashlib.sha1(json.dumps([l["title"], l["description"], l["images"]]).encode()).hexdigest()[:12]


def ai_result(entry, cfg, content=None):
    """The triage result for the current provider setting and listing content, or None if not (fully) triaged."""
    rs = [e["result"] if (e := entry.get(p, {})).get("prompt") == AI_PROMPT_VERSION
          and (content is None or e.get("content") == content) else None for p in ai_providers(cfg)]
    return None if not all(rs) else rs[0] if len(rs) == 1 else consensus(*rs)


def ai_reasons(r, ex):
    """AI findings that exclude a listing under the user's triage settings."""
    has = lambda f: flagged(r, f)
    reasons = [f for f in ("furnished", "swap", "shared") if ex[f] and has(f)]
    if ex["temporary"] and has("temporary") and not (
            ex["temporary_min_until"] and (r["temporary_until"] or "") >= ex["temporary_min_until"]):
        reasons.append(f"temporary until {r['temporary_until']}" if r["temporary_until"] else "temporary")
    if ex["kitchen"] and r["kitchen"] in KITCHEN_SIZES and KITCHEN_SIZES.index(r["kitchen"]) <= KITCHEN_SIZES.index(ex["kitchen"]):
        reasons.append(f"{r['kitchen']} kitchen")
    return reasons + (matched_exclusions(r) if ex["custom"] else [])


# ---------- one flat on several portals ----------

SITE_RANK = ("homegate", "immoscout24", "flatfox", "newhome")  # whose copy is shown when a flat is on several portals
DUP_METERS = 150  # how far apart two copies' coordinates may be (portals geocode differently)
DUP_PHOTOS = 5  # photos per listing compared; dedup.py hashes these
PHOTO_BITS = 8  # differing hash bits under which two photos are the same picture (resized/recompressed)
_clusters = {}  # key -> all keys of its flat (incl. itself), from the last annotate(); update_notes() edits them together


def street_key(address):
    """'Käferholzstr. 56' and 'Käferholzstrasse 56' -> 'kaferholz str 56'; None without an address."""
    a = unicodedata.normalize("NFKD", (address or "").lower())
    a = "".join(c for c in a if not unicodedata.combining(c)).replace("ß", "ss")
    a = re.sub(r"[^a-z0-9]+", " ", re.sub(r"str(asse|\.)", " str ", a)).strip()
    return a or None


def numbered(key):
    return bool(key and re.search(r"\d", key))


def shingles(text):
    words = re.findall(r"\w+", re.sub(r"<[^>]+>", " ", text or "").lower())
    return {" ".join(words[i:i + 3]) for i in range(len(words) - 2)}


def text_sim(a, b):
    """Share of shared 3-word phrases between two descriptions (0..1); None when either has none."""
    return len(a & b) / len(a | b) if a and b else None


def near(x, y, slack, rel):
    return x is None or y is None or abs(x - y) <= max(slack, rel * max(x, y))


def meters(a, b):
    return math.hypot((a["lat"] - b["lat"]) * 111_320, (a["lon"] - b["lon"]) * 111_320 * math.cos(math.radians(a["lat"])))


def dup_candidates(pool):
    """Pairs of listings on different portals that could be one flat: same rooms, area and rent close, and the same
    street address or coordinates within DUP_METERS. Cheap, so the photo comparison only runs on these."""
    by_rooms, out = {}, set()
    for k, l in pool.items():
        by_rooms.setdefault(l["rooms"], []).append(k)
    for keys in by_rooms.values():
        keys.sort(key=lambda k: pool[k]["lat"] or 0)
        streets = {}
        for k in keys:
            if numbered(sk := street_key(pool[k]["address"])):
                streets.setdefault(sk, []).append(k)
        pairs = [(a, b) for ks in streets.values() for i, a in enumerate(ks) for b in ks[i + 1:]]
        located = [k for k in keys if pool[k]["lat"] is not None]
        for i, a in enumerate(located):  # sorted by latitude: stop once the next one is too far north
            for b in located[i + 1:]:
                if (pool[b]["lat"] - pool[a]["lat"]) * 111_320 > DUP_METERS:
                    break
                if meters(pool[a], pool[b]) <= DUP_METERS:
                    pairs.append((a, b))
        for a, b in pairs:
            la, lb = pool[a], pool[b]
            if la["site"] != lb["site"] and near(la["area_m2"], lb["area_m2"], 3, .06) and near(la["rent_chf"], lb["rent_chf"], 50, .12):
                out.add((a, b) if a < b else (b, a))
    return sorted(out)


def hamming(a, b):
    return (int(a, 16) ^ int(b, 16)).bit_count()


def dup_score(la, lb, ha, hb, texts):
    """How sure we are that two candidate listings are one flat (0 = not the same).
    Photos decide when both have hashed photos; without them (or when the portals show different photos) an
    identical address, area and rent plus a matching description does. Different house numbers need two photos."""
    sa, sb = street_key(la["address"]), street_key(lb["address"])
    clash = numbered(sa) and numbered(sb) and sa != sb
    same = numbered(sa) and sa == sb
    photos = sum(any(hamming(x, y) <= PHOTO_BITS for y in hb) for x in ha) if ha and hb else None
    if photos and photos >= (2 if clash else min(2, len(ha), len(hb))):
        return 10 * photos + (texts or 0)
    exact = near(la["area_m2"], lb["area_m2"], 1, 0) and near(la["rent_chf"], lb["rent_chf"], 0, .02)
    if not clash and exact and texts is not None and texts >= (0.5 if same and photos is None else 0.8):
        return 1 + texts
    return 0


def find_duplicates(pool, photos):
    """Group the listings in pool that are one flat. -> {key: [keys of its flat, shown copy first]} for flats on more
    than one portal. homegate and immoscout24 share ids (and flatfox knows the homegate id), so those always group;
    the rest is matched by dup_candidates() + dup_score(). A flat has at most one listing per portal."""
    links = [(99, a, b) for a in pool for b in (
        [f"homegate:{pool[a]['id']}"] if pool[a]["site"] == "immoscout24" else
        [f"{s}:{pool[a]['smg_id']}" for s in ("homegate", "immoscout24")] if pool[a].get("smg_id") else []) if b in pool]
    hashes = {k: [photos[u] for u in l["images"][:DUP_PHOTOS] if photos.get(u)] for k, l in pool.items()}
    shared = {}  # a photo on several listings of one portal (a project rendering, the building) tells flats apart badly
    for k, hs in hashes.items():
        for h in set(hs):
            shared.setdefault((pool[k]["site"], h), set()).add(k)
    for k in hashes:
        hashes[k] = [h for h in hashes[k] if len(shared[(pool[k]["site"], h)]) == 1]
    texts, cands = {}, dup_candidates(pool)
    for k in {k for pair in cands for k in pair}:
        texts[k] = shingles(pool[k]["description"])
    for a, b in cands:
        if score := dup_score(pool[a], pool[b], hashes[a], hashes[b], text_sim(texts[a], texts[b])):
            links.append((score, a, b))
    group = {k: [k] for k in pool}
    for _, a, b in sorted(links, reverse=True):  # surest first; a link that would put two of one portal together loses
        ga, gb = group[a], group[b]
        if ga is not gb and not {pool[k]["site"] for k in ga} & {pool[k]["site"] for k in gb}:
            ga += gb
            for k in gb:
                group[k] = ga
    rank = lambda k: (SITE_RANK.index(pool[k]["site"]), pool[k].get("first_seen") or "", k)
    return {k: sorted(g, key=rank) for k, g in group.items() if len(g) > 1}


def merge_notes(entries):
    """Your notes on all copies of one flat (the shown copy's first) as one: furthest status, starred if any copy is,
    all tags and notes. Pinned photos are the shown copy's: the other portals' photo URLs differ."""
    out = {**entries[0]}
    if len(entries) > 1:
        out["status"] = max((e.get("status", "") for e in entries), key=STATUSES.index)
        out["starred"] = any(e.get("starred") for e in entries)
        out["tags"] = list(dict.fromkeys(t for e in entries for t in e.get("tags") or []))
        out["note"] = "\n\n".join(dict.fromkeys(e["note"].strip() for e in entries if (e.get("note") or "").strip()))
        out["seen"] = min((e["seen"] for e in entries if e.get("seen")), default="")
        out["updated"] = max(e.get("updated", "") for e in entries)
    return {k: v for k, v in out.items() if v not in ("", [], False, None)}


def in_polygon(lat, lon, points):
    """Ray casting; points are [[lat, lon], ...] (small areas, so degrees work as plane coordinates)."""
    inside = False
    for (y1, x1), (y2, x2) in zip(points, points[-1:] + points[:-1]):
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def excluding_area(l, areas):
    if l.get("lat") is None or l.get("lon") is None:
        return None
    return next((a for a in areas if a.get("on", True) and len(a["points"]) > 2
                 and in_polygon(l["lat"], l["lon"], a["points"])), None)


def annotate(listings, state):
    return overlay(annotate_base(listings, state), state["hidden"], load(NOTES, {}))


def flat_of(key, listings):
    """All keys of the flat a listing belongs to, shown copy first (from annotate_base's "dups" and "dup_of")."""
    l = listings[key]
    shown = l.get("dup_of") or key
    return [shown, *listings[shown].get("dups", [])]


def user_part(key, flat, hidden, notes):
    """-> (your notes for this listing, whether hiding excludes it). Cheap, so the UI can redo it on every edit.
    The shown copy of a flat carries your notes from all copies. Hiding any copy excludes the flat, unless you gave
    a copy you didn't hide a status or star (you kept that one)."""
    user = merge_notes([notes.get(k, {}) for k in flat]) if key == flat[0] else notes.get(key, {})
    kept = any(k not in hidden and (notes.get(k, {}).get("status") or notes.get(k, {}).get("starred")) for k in flat)
    return user, bool(hidden & set(flat)) and not kept


def overlay(listings, hidden, notes):
    """Your own part on top of annotate_base(): notes as "user", and hidden listings excluded."""
    hidden = set(hidden)
    for key, l in listings.items():
        l["user"], h = user_part(key, flat_of(key, listings), hidden, notes)
        if h:
            l["excluded"] = "hidden"
    return listings


def annotate_base(listings, state):
    """Everything except overlay(): duplicates, exclusions, AI, commute, taxes. state["hidden"] is not used."""
    sites = state["search"]["sites"]
    locs = {x["id"] for x in state["search"]["locations"]}
    current = lambda l: not l.get("deleted_at") and bool(set(l.get("found_in") or []) & locs)
    add_providers(listings, state["provider_labels"])
    for l in listings.values():
        other = {"homegate": "immoscout24", "immoscout24": "homegate"}.get(l["site"])
        l["conflicts"] = conflicts(l, listings.get(f"{other}:{l['id']}") if other else None)
    ai = load(TRIAGE, {})
    routes, points = load(COMMUTE, {}), state["commute"]["points"]
    # one flat on several portals: the best-ranked copy is shown (overlay() gives it your notes from all copies)
    global _clusters
    blocks = {}
    blocked = lambda k: blocks[k] if k in blocks else blocks.setdefault(k, blocked_by(listings[k], state["blocklist"]))
    _clusters = find_duplicates({k: l for k, l in listings.items() if l["site"] in sites and current(l)}, load(PHOTOS, {}))
    for key, l in listings.items():
        flat = _clusters.get(key, [key])
        if len(flat) > 1:
            if key == flat[0]:
                l["dups"] = flat[1:]
                l["also_on"] = [{"key": k, "site": listings[k]["site"], "url": listings[k]["url"]} for k in flat[1:]]
            else:
                l["dup_of"] = flat[0]
        l["ai"] = ai_result(ai.get(key, {}), state["triage"], content_hash(l))
        if not l["available_from"] and l["ai"] and l["ai"]["available_from"]:  # homegate/immoscout24: from the text
            l["available_from"], l["available_source"] = l["ai"]["available_from"], "AI"
        # blocking any copy of a flat (say newhome names the agency, homegate only shows its logo) excludes it
        b = next((x for k in flat if (x := blocked(k))), None)
        l["excluded"] = (
            "site not selected" if l["site"] not in sites else
            "deleted" if l.get("deleted_at") else
            "other search" if not current(l) else
            f"duplicate of {listings[flat[0]]['site']}" if key != flat[0] else
            f"blocked: {b}" if b else
            fails_search(l, state["search"]))
        if not l["excluded"] and (a := excluding_area(l, state["exclusion_areas"])):  # before commute/AI: no API calls
            l["excluded"] = f"area: {a['name']}"
        # per destination: best route, "no route", or None (not computed yet)
        l["commute"] = [commute_result(l, p, routes) for p in points]
        if not l["excluded"] and (reasons := commute_reasons(l, points)):
            l["excluded"] = "commute: " + ", ".join(reasons)
        if not l["excluded"] and l["ai"] and (reasons := ai_reasons(l["ai"], state["triage"]["exclude"])):
            l["excluded"] = "AI: " + ", ".join(reasons)
    add_taxes(listings, state["tax"])
    return listings


def write_eligible(state):
    listings = annotate(load_listings(), state)
    save(ELIGIBLE, [{k: v for k, v in l.items() if k != "excluded"} for l in listings.values() if not l["excluded"]])
    return listings


# ---------- your notes ----------

STATUSES = ("", "maybe", "shortlist", "contacted", "viewing", "applied")  # "" = inbox; rejecting = hiding
NOTE_FIELDS = {"status": str, "starred": bool, "tags": list, "note": str, "pinned": list, "seen": str}


def set_hidden(keys, hidden):
    """Hide (reject) or unhide listings in state.json; returns the new hidden list."""
    state = load(STATE, {})
    keep = [k for k in state.get("hidden", []) if k not in keys]
    state["hidden"] = keep + list(dict.fromkeys(keys)) if hidden else keep
    save(STATE, state)
    return state["hidden"]


def update_notes(keys, patch, now=None):
    """Merge patch into each listing's notes; unknown fields and statuses are ignored, empty values dropped.
    "updated" tracks your edits, so marking a listing seen doesn't bump it."""
    patch = {k: v for k, v in patch.items() if isinstance(v, NOTE_FIELDS.get(k, ()))
             and not (k == "status" and v not in STATUSES)}
    for k in ("tags", "pinned"):  # lists of strings, in order, no repeats
        if k in patch:
            patch[k] = list(dict.fromkeys(x.strip() for x in patch[k] if isinstance(x, str) and x.strip()))
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    notes = load(NOTES, {})
    # all copies of a flat get the edit (pinned photos are per portal: their URLs differ)
    for key in dict.fromkeys(k for key in keys for k in _clusters.get(key, [key])):
        p = patch if key in keys else {f: v for f, v in patch.items() if f != "pinned"}
        if not p:
            continue
        entry = {**notes.get(key, {}), **p}
        if set(p) - {"seen"}:
            entry["updated"] = now
        entry = {k: v for k, v in entry.items() if v not in ("", [], False, None)}
        if set(entry) - {"updated"}:
            notes[key] = entry
        else:
            notes.pop(key, None)
    if set(patch) - {"seen"}:  # marking seen happens on every click; your edits are what's worth undoing
        backup_notes()
    save(NOTES, notes)
    return {k: notes.get(k, {}) for k in keys}


def backup_notes():
    """Copy notes.json aside before an edit overwrites it, so a bad save can be rolled back by hand."""
    if not NOTES.exists():
        return
    NOTES_BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    shutil.copy2(NOTES, NOTES_BACKUPS / f"notes-{stamp}.json")
    for old in sorted(NOTES_BACKUPS.glob("notes-*.json"))[:-NOTES_KEEP]:
        old.unlink()


# ---------- scraping ----------

def smg_url(site, slug, s, page):
    q = {"ep" if site == "homegate" else "pn": page, "o": "dateCreated-desc"}
    if site == "homegate":
        q |= {"ac": s["min_rooms"], "ak": s["min_area"], "ah": s["max_rent"]}
        base = f"https://www.homegate.ch/rent/apartment/{slug}/matching-list"
    else:
        q |= {"nrf": s["min_rooms"], "slf": s["min_area"], "pt": s["max_rent"]}
        base = f"https://www.immoscout24.ch/en/real-estate/rent/{slug}"
    return base + "?" + urllib.parse.urlencode({k: v for k, v in q.items() if v})


def wait_for(page, fn, log, what):
    """Poll fn() until truthy; nudge the user to solve a captcha if it takes a while."""
    start, nagged = time.time(), False
    while time.time() - start < CAPTCHA_TIMEOUT:
        if page.is_closed():
            raise RuntimeError("browser closed")
        try:
            if r := fn():
                return r
        except Exception:
            pass  # page mid-navigation (captcha redirect etc.)
        if not nagged and time.time() - start > 8:
            log(f"  waiting for {what}: solve the captcha in the browser window if there is one")
            nagged = True
        page.wait_for_timeout(1000)
    log(f"  gave up waiting for {what}")
    return None


def goto(page, url, log):
    """Navigate, backing off on rate limiting (429/503): 30s, 60s, 120s. Captchas are left to wait_for()."""
    for attempt in range(4):
        resp = page.goto(url, wait_until="domcontentloaded")
        if not resp or resp.status not in (429, 503) or attempt == 3:
            return resp
        wait = 30 * 2 ** attempt * random.uniform(0.8, 1.2)
        log(f"  rate limited (HTTP {resp.status}), waiting {wait:.0f}s")
        page.wait_for_timeout(wait * 1000)


def pause(page, lo, hi):
    """Randomised delay between requests: gentler on the sites and less bot-like than a fixed interval."""
    page.wait_for_timeout(random.uniform(lo, hi) * 1000)


STATE_JS = """() => { const s = window.__INITIAL_STATE__ || window.__PINIA_INITIAL_STATE__;
    return s ? JSON.stringify(s) : null }"""


def scrape_smg(page, site, loc, s, log, known=(), quick=False, done=None):
    """Yield search results (newest first). quick: stop at the first page without new ids.
    done["complete"] is set when every result page was read (needed to detect removed listings)."""
    seen = set()
    for n in range(1, s["max_pages"] + 1):
        url = smg_url(site, loc[site], s, n)
        log(f"{site} {loc['label']} page {n}: {url}")
        resp = goto(page, url, log)
        if resp and resp.status == 404:
            log(f"  {site} doesn't know location {loc[site]!r}")
            return
        # The SSR state exists once the real page (not a captcha) has loaded; it may hold 0 listings.
        if not (state := wait_for(page, lambda: page.evaluate(STATE_JS), log, "search results")):
            return
        state = json.loads(state)
        items = find_listings(state)
        ids = {str(x.get("listing", x)["id"]) for x in items}
        result = state.get("resultList", {}).get("search", {}).get("fullSearch", {}).get("result", {})
        if not ids - seen:  # empty or repeated page: past the end
            done is not None and done.update(complete=True)
            return
        seen |= ids
        yield from items
        if n >= (result.get("pageCount") or n + 1):
            done is not None and done.update(complete=True)
            return
        if quick and ids <= set(known):
            log(f"  page {n} has no new listings; stopping (quick scan)")
            return
        pause(page, 2, 4)
    log(f"  stopped at max pages ({s['max_pages']}); removed listings can't be detected for {loc['label']}")


FETCH_JS = """u => fetch(u, {credentials: 'include', headers: {Accept: 'application/json'}})
    .then(r => r.ok ? r.json().catch(() => ({error: 'not json'})) : {error: r.status})
    .catch(e => ({error: String(e)}))"""


def fetch_json(page, url):
    """GET url from inside the page; {"error": ...} on failure (a Cloudflare challenge page, a navigation mid-call)
    instead of an exception that would abort the whole scrape."""
    try:
        return page.evaluate(FETCH_JS, url)
    except Exception as e:
        if page.is_closed():
            raise
        return {"error": repr(e)}


# A banner in the browser window: newhome is read through its API, so the visible page never changes on its own
# and a scrape that is working looks stuck.
PROGRESS_JS = """t => { document.title = t;
    let b = document.getElementById('scraper-progress');
    if (!b) { b = document.createElement('div'); b.id = 'scraper-progress';
        b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#1a7f37;color:#fff;'
            + 'font:600 16px system-ui,sans-serif;padding:12px;text-align:center';
        document.body.appendChild(b); }
    b.textContent = t; }"""


def show_progress(page, text):
    try:
        page.evaluate(PROGRESS_JS, text)
    except Exception:
        if page.is_closed():
            raise  # mid-navigation otherwise: the next update redraws it


def newhome_description(page, immocode):
    """Search results lack the description; fetch it from the detail API (None on failure, retried next scrape)."""
    r = fetch_json(page, f"https://service.newhome.ch/api/api/ListingDetailRequest?languageIso=de&immocode={immocode}")
    pause(page, 0.5, 1.5)
    return (r.get("detail") or {}).get("descriptionTextPlain") if "error" not in r else None


# The "Available from:" / "Availability:" <dt> on homegate/immoscout24 detail pages; null until the page has loaded.
DETAIL_JS = """() => { const dts = [...document.querySelectorAll('dt')];
    if (!dts.length) return null;
    const dt = dts.find(d => /avail|verfügbar|bezug|disponib/i.test(d.textContent));
    return dt && dt.nextElementSibling ? dt.nextElementSibling.textContent.trim() : ''; }"""


def parse_available(text):
    if re.search(r"immediat|sofort|de suite", text, re.I):
        return "immediately"
    if re.search(r"agreement|vereinbarung|convenir|arrangement", text, re.I):
        return "by arrangement"
    if m := re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text):
        return f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"
    return None


# The listing object in the detail page's state (cycle-safe walk: the state is a live store).
LISTING_JS = """id => { const seen = new Set();
    const walk = (o, d) => { if (!o || typeof o !== 'object' || seen.has(o) || d > 12) return null; seen.add(o);
        if (String(o.id) === id && o.characteristics && o.localization) return o;
        for (const v of Object.values(o)) { const r = walk(v, d + 1); if (r) return r; } return null; };
    for (const root of [window.__INITIAL_STATE__, window.__PINIA_INITIAL_STATE__, window.__NUXT__]) {
        const hit = walk(root, 0); if (hit) return JSON.parse(JSON.stringify(hit)); }
    return null; }"""


def gone_from(page, lid):
    """Removed listings redirect to a search page, e.g. homegate's .../matching-list?expired=<id>."""
    u = urllib.parse.urlsplit(page.url)
    return u.scheme.startswith("http") and ("expired=" in u.query or not re.search(rf"/rent/{lid}/?$", u.path))


def read_detail(page, lid):
    return {"available": parse_available(page.evaluate(DETAIL_JS) or ""), "listing": page.evaluate(LISTING_JS, lid),
            "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def smg_detail(page, site, lid, log):
    """{"available", "listing", "fetched"} from the listing's own page, or None if it didn't load (retried later)."""
    goto(page, f"https://www.{site}.ch/rent/{lid}", log)
    if not wait_for(page, lambda: gone_from(page, lid) or page.evaluate(DETAIL_JS) is not None, log, "listing page"):
        return None
    if gone_from(page, lid):
        log(f"  {site}:{lid} was removed from the site")
        return {"removed": True, "available": None}
    detail = read_detail(page, lid)
    pause(page, 1, 2.5)
    return detail


def smg_check(page, site, lid, log):
    """Is a listing that vanished from the results removed? (True, None) on 404/410 or a redirect away from it,
    (False, detail) if its page still shows it, (None, None) if unclear (kept; checked again next full scan)."""
    try:
        resp = goto(page, f"https://www.{site}.ch/rent/{lid}", log)
        if resp and resp.status in (404, 410):
            return True, None
        if resp and resp.status == 403:  # captcha: let the user solve it
            wait_for(page, lambda: gone_from(page, lid) or page.evaluate(DETAIL_JS) is not None, log, "listing page")
        for _ in range(20):
            if gone_from(page, lid):
                return True, None
            try:
                if page.evaluate(DETAIL_JS) is not None:
                    return False, read_detail(page, lid)
            except Exception:
                pass  # mid-navigation
            page.wait_for_timeout(1000)
        return None, None
    finally:
        pause(page, 1, 2.5)


def newhome_check(page, immocode):
    """Like smg_check, via newhome's detail API (the page is already on newhome after its search)."""
    r = fetch_json(page, f"https://service.newhome.ch/api/api/ListingDetailRequest?languageIso=de&immocode={immocode}")
    pause(page, 0.5, 1.5)
    if r.get("error") in (404, 410):
        return True, None
    if "error" in r or not r.get("detail"):
        return None, None
    return False, {"descriptionTextPlain": r["detail"].get("descriptionTextPlain")}


def check_removed(page, site, locs, listings, now, checked, log):
    """Listings these completely scanned searches found before but not now: removed, or only outside the filters?"""
    gone = [k for k, l in listings.items() if l["site"] == site and not l.get("deleted_at")
            and l["last_seen"] < now and set(l.get("found_in") or []) & set(locs)]
    if gone:
        log(f"{site}: {len(gone)} listing(s) no longer in the results; checking whether they were removed")
    for k in gone:
        lid = k.split(":", 1)[1]
        if lid not in checked:  # homegate and immoscout24 share ids: check once
            checked[lid] = (flatfox_check(lid) if site == "flatfox" else newhome_check(page, lid) if site == "newhome"
                            else smg_check(page, site, lid, log))
        deleted, detail = checked[lid]
        if deleted:
            listings[k]["deleted_at"] = now
            log(f"  {k}: removed -> deleted")
        elif detail:  # still online (e.g. rent changed, now outside your filters): keep it with fresh data
            listings[k]["raw"]["detail"] = detail
            listings[k] |= {**normalize(site, listings[k]["raw"]), "last_seen": now}
            log(f"  {k}: still online")
        else:
            log(f"  {k}: couldn't tell; checking again next full scan")


def refresh(keys, log=print):
    """Re-read the given homegate/immoscout24 listings from their own pages (paced, only what's asked for)."""
    listings = load(LISTINGS, {})
    keys = [k for k in keys if k in listings and k.split(":")[0] in ("homegate", "immoscout24")]
    log(f"Refreshing {len(keys)} listing page(s)")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, headless=False, channel="chrome", no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            for i, k in enumerate(keys, 1):
                site, lid = k.split(":")
                if detail := smg_detail(page, site, lid, log):
                    for twin in (f"homegate:{lid}", f"immoscout24:{lid}"):  # same listing on both portals
                        if twin in listings:
                            listings[twin]["raw"]["detail"] = detail
                            if detail.get("removed"):  # keep the date it was first found removed
                                listings[twin]["deleted_at"] = (listings[twin].get("deleted_at")
                                                                or datetime.now(timezone.utc).isoformat(timespec="seconds"))
                            else:  # online again
                                listings[twin].pop("deleted_at", None)
                    if detail.get("removed"):
                        continue
                    n = normalize(site, listings[k]["raw"])
                    log(f"  {i}/{len(keys)} {k}: {n['rooms']} rooms, {n['area_m2']} m², CHF {n['rent_chf']}")
                else:
                    log(f"  {i}/{len(keys)} {k}: page didn't load")
        except Exception as e:
            log(f"stopped: {e!r} (results so far are kept)")
        finally:
            ctx.close()
            save(LISTINGS, listings)


def scrape_newhome(page, loc, s, log, known=(), quick=False, done=None):
    if not loc["newhome"]:
        log(f"newhome has no ZIP search; skipping {loc['label']} (add the town instead)")
        return
    url = f"https://www.newhome.ch/de/mieten/suchen/wohnung/{loc['newhome']}/liste"
    api = []

    def on_request(r):
        if "SearchListingRequest" in r.url:
            api.append(r.url)

    def load_page():  # -> captured API url, or None if newhome redirected away (unknown location)
        api.clear()
        goto(page, url, log)
        wait_for(page, lambda: api or "/liste" not in page.url, log, "newhome search")
        return api[-1] if api else None

    page.on("request", on_request)
    try:
        log(f"newhome {loc['label']}: {url}")
        if not (api_url := load_page()):
            log(f"  newhome doesn't know location {loc['newhome']!r}")
            return
        # Re-issue the app's own API call (it knows the location id) with our filters + pagination.
        u = urllib.parse.urlsplit(api_url)
        q = dict(urllib.parse.parse_qsl(u.query)) | {"order": "1", "rowCount": "20", "numberOfSpecialPromotions": "0"}
        q |= {k: str(v) for k, v in [("roomsMin", s["min_rooms"]), ("livingAreaMin", s["min_area"]),
                                     ("priceMax", s["max_rent"])] if v}
        total = 0
        for n in range(s["max_pages"]):
            q["skipCount"] = str(n * 20)  # API caps rowCount at 20
            log(f"newhome {loc['label']} page {n + 1}" + (f" of {pages}" if n else ""))
            for attempt in range(5):
                r = fetch_json(page, u._replace(query=urllib.parse.urlencode(q)).geturl())
                if "error" not in r:
                    break
                # Cloudflare intermittently challenges the API; reloading the page clears it. Back off: 5, 10, 20, 40s.
                log(f"  newhome API error {r['error']}, reloading (solve captcha if shown)")
                pause(page, 5 * 2 ** attempt * 0.8, 5 * 2 ** attempt * 1.2)
                load_page()
            else:
                return
            entries = r.get("entries") or []
            count = r.get("totalResultCount") or 0
            pages = -(-count // 20)
            if not n:
                log(f"  {count} listings match")
            for i, e in enumerate(entries):
                show_progress(page, f"Scraping newhome {loc['label']}: listing {total + i + 1} of {count} "
                                    "(keep this window open)")
                yield e
            total += len(entries)
            if not entries or total >= count:
                done is not None and done.update(complete=True)
                return
            # Only trust an all-known page once this search has been read to the end before (an interrupted full
            # scan leaves later pages unread, and every quick scan would otherwise stop at page 1 forever).
            if quick and len(known) >= count and {str(e["immocode"]) for e in entries} <= set(known):
                log(f"  page {n + 1} has no new listings; stopping (quick scan)")
                return
            pause(page, 1, 2.5)
    finally:
        page.remove_listener("request", on_request)


# ---------- flatfox (public JSON API, no browser) ----------

def get_json(url, params=()):
    req = urllib.request.Request(url + ("?" + urllib.parse.urlencode(params) if params else ""),
                                 headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503) or attempt == 3:
                raise
            time.sleep(10 * 2 ** attempt * random.uniform(0.8, 1.2))


def area(loc, log):
    """[west, south, east, north] of a location, from swisstopo's geo search (cached)."""
    areas = load(AREAS, {})
    if loc["id"] not in areas:
        kind = loc["id"].split("-")[1]  # city, region, canton, zipcode, cityregion
        name = re.sub(r"\s*\(.*\)$", "", loc["label"])
        text, origin = {"zipcode": (name.split()[0], "zipcode"), "region": (name, "district"),
                        "canton": (name, "kantone")}.get(kind, (name, "gg25"))
        r = get_json("https://api3.geo.admin.ch/rest/services/api/SearchServer",
                     {"type": "locations", "searchText": text, "origins": origin, "sr": 4326, "limit": 5})
        boxes = [x["attrs"]["geom_st_box2d"] for x in r.get("results", [])
                 if re.sub(r"<[^>]+>|\s*\(.*\)", "", x["attrs"].get("label", "")).strip().lower() == text.lower()
                 or origin == "zipcode"]
        if not boxes:
            log(f"  flatfox: no map area found for {loc['label']}")
            return None
        areas[loc["id"]] = [float(v) for v in re.findall(r"[\d.]+", boxes[0])]
        save(AREAS, areas)
    return areas[loc["id"]]


def in_location(zip_, loc_id):
    """Whether a ZIP lies in a location (any of its places), via the homegate geo hierarchy (cached)."""
    zips = load(ZIPS, {})
    if zip_ not in zips:
        res = get_json("https://api.homegate.ch/geo/locations", {"lang": "de", "size": 5, "name": zip_})["results"]
        g = next((x["geoLocation"] for x in res if x["geoLocation"]["id"] == f"geo-zipcode-{zip_}"), None)
        zips[zip_] = sorted({g["id"], *(p for path in g.get("parentPaths", []) for p in path)}) if g else []
        save(ZIPS, zips)
        time.sleep(0.2)
    return loc_id in zips[zip_]


def scrape_flatfox(loc, s, log, known=(), quick=False, done=None):
    """All flatfox listings in a location: one pin request lists every match (newest first); only new ones'
    details are fetched (cached in data/flatfox.json). done["complete"] unless the pin limit was hit."""
    if not (box := area(loc, log)):
        return
    filters = {k: v for k, v in (("min_rooms", s["min_rooms"]), ("max_price", s["max_rent"])) if v}
    log(f"flatfox {loc['label']}: map area {box[0]:.3f},{box[1]:.3f} – {box[2]:.3f},{box[3]:.3f}")
    pks, complete = flatfox_pins(box, filters)
    pks = sorted(pks, reverse=True)
    if done is not None and complete:
        done["complete"] = True
    elif not complete:
        log("  flatfox: some map tiles still hit the 400-result limit; a few listings may be missing")
    cache = load(FLATFOX, {})
    new = [pk for pk in pks if str(pk) not in cache]
    log(f"  {len(pks)} in the map area, {len(new)} not fetched before")
    for i in range(0, len(new), 48):
        batch = new[i:i + 48]
        log(f"  fetching details {i + 1}–{i + len(batch)} of {len(new)}")
        r = get_json("https://flatfox.ch/api/v1/public-listing/",
                     [("pk", pk) for pk in batch] + [("limit", len(batch)), ("expand", "images")])
        cache |= {str(x["pk"]): x for x in r.get("results", [])}
        save(FLATFOX, cache)
        time.sleep(random.uniform(1, 2.5))
    # the map area is a rectangle: keep listings whose ZIP really belongs to the location
    zips = load(ZIPS, {})
    if unseen := {str(x["zipcode"]) for pk in pks if (x := cache.get(str(pk))) and x.get("zipcode")} - zips.keys():
        log(f"  looking up {len(unseen)} new ZIP codes to keep only listings inside {loc['label']}")
    for pk in pks:
        if (x := cache.get(str(pk))) and x.get("zipcode") and in_location(str(x["zipcode"]), loc["id"]):
            yield x


def flatfox_pins(box, filters, depth=0):
    """({pk}, complete) for a map area. The API returns at most 400 pins, so a full area is split into quarters."""
    west, south, east, north = box
    pins = get_json("https://flatfox.ch/api/v1/pin/", {"west": west, "south": south, "east": east, "north": north,
                                                         "max_count": 400, "offer_type": "RENT",
                                                         "object_category": "APARTMENT", **filters})
    if len(pins) < 400:
        return {p["pk"] for p in pins}, True
    if depth >= 3:
        return {p["pk"] for p in pins}, False
    mx, my = (west + east) / 2, (south + north) / 2
    pks, complete = set(), True
    for quarter in ((west, south, mx, my), (mx, south, east, my), (west, my, mx, north), (mx, my, east, north)):
        time.sleep(random.uniform(0.5, 1.5))
        q, c = flatfox_pins(quarter, filters, depth + 1)
        pks, complete = pks | q, complete and c
    return pks, complete


def flatfox_check(pk):
    """(True, None) if a flatfox listing is gone, (False, None) if it's still published, (None, None) if unclear."""
    try:
        r = get_json("https://flatfox.ch/api/v1/public-listing/", [("pk", pk), ("limit", 1)])
    except Exception:
        return None, None
    time.sleep(random.uniform(0.5, 1.5))
    hits = [x for x in r.get("results", []) if str(x["pk"]) == str(pk)]
    return (not hits or hits[0].get("status") not in (None, "act", "active", "published")), None


# ---------- main ----------

def scrape(state, log=print, quick=False, sites=None):
    """quick: per location, stop at the first result page with no new listings. Full: every result page, then
    listings these searches found before but not now are checked and moved to "deleted" if removed.
    sites: only these portals (default: the selected ones); others' listings are left untouched."""
    s = state["search"]
    sites = [x for x in (sites or s["sites"]) if x in DEFAULT_STATE["search"]["sites"]]
    listings = load_listings(cached=False)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log(("Quick scan: new listings only" if quick else "Full scan: all result pages, then check vanished listings")
        + f" ({', '.join(sites)})")
    checked = {}

    def run_site(site, search, page=None):
        complete = []
        for loc in s["locations"]:
            n, new, done = 0, 0, {}
            known = {k.split(":", 1)[1] for k, l in listings.items()  # already found by this search
                     if l["site"] == site and not l.get("deleted_at") and loc["id"] in (l.get("found_in") or [])}
            try:
                for x in search(loc, known, done):
                    if site in ("homegate", "immoscout24"):  # availability is only on the detail page; once per listing
                        lid = str(x.get("listing", x)["id"])
                        old = next((d for k in (f"{site}:{lid}", f"homegate:{lid}", f"immoscout24:{lid}")
                                    if "available" in (d := listings.get(k, {}).get("raw", {}).get("detail") or {})), None)
                        if detail := old or smg_detail(page, site, lid, log):
                            x["detail"] = detail
                    if site == "newhome":
                        old = listings.get(f"newhome:{x['immocode']}", {}).get("raw", {}).get("detail") or {}
                        x["detail"] = old if old.get("descriptionTextPlain") else {
                            "descriptionTextPlain": newhome_description(page, x["immocode"])}
                    l = normalize(site, x)
                    key = f"{site}:{l['id']}"
                    prev = listings.get(key, {})  # a reappearing deleted listing comes back to life
                    listings[key] = {**l, "first_seen": prev.get("first_seen", now), "last_seen": now,
                                     "found_in": sorted(set(prev.get("found_in") or []) | {loc["id"]})}
                    if (x.get("detail") or {}).get("removed"):  # still in the results, page says expired
                        listings[key]["deleted_at"] = prev.get("deleted_at") or now
                    n, new = n + 1, new + (not prev or bool(prev.get("deleted_at")))
            except Exception as e:  # one broken site shouldn't kill the run
                if page is not None and page.is_closed():
                    raise
                log(f"{site} {loc['label']} failed: {e!r}")
            log(f"{site} {loc['label']}: {n} listings, {new} new")
            if done.get("complete"):
                complete.append(loc["id"])
        if not quick and complete:
            try:
                check_removed(page, site, complete, listings, now, checked, log)
            except Exception as e:
                if page is not None and page.is_closed():
                    raise
                log(f"{site}: checking removed listings failed: {e!r}")

    try:
        if "flatfox" in sites:
            run_site("flatfox", lambda loc, known, done: scrape_flatfox(loc, s, log, known, quick, done))
        if browser_sites := [x for x in sites if x != "flatfox"]:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    PROFILE, headless=False, channel="chrome", no_viewport=True,
                    args=["--disable-blink-features=AutomationControlled"])
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    for site in browser_sites:
                        run_site(site, lambda loc, known, done, site=site: (
                            scrape_newhome(page, loc, s, log, known, quick, done) if site == "newhome"
                            else scrape_smg(page, site, loc, s, log, known, quick, done)), page)
                except Exception as e:
                    log("browser was closed; stopping (results so far are kept)" if page.is_closed()
                        else f"stopped: {e!r} (results so far are kept)")
                finally:
                    ctx.close()
    finally:
        save(LISTINGS, listings)
    eligible = sum(not l["excluded"] for l in write_eligible(load_state()).values())  # settings saved during the run count
    log(f"done: {len(listings)} total, {eligible} eligible -> {ELIGIBLE}")


if __name__ == "__main__":
    scrape(load_state())
    import dedup  # imports this module
    dedup.run(load_state())
