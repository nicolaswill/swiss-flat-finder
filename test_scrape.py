"""python test_scrape.py — offline check of extraction + filtering."""
import json
import tempfile
from pathlib import Path

import scrape

scrape.NOTES_BACKUPS = Path(tempfile.mkdtemp()) / "notes"  # never back up into the real data/ from a test

smg = {"listing": {"id": 1, "prices": {"rent": {"gross": 2500}}, "characteristics": {"numberOfRooms": 3.5, "livingSpace": 70},
                   "address": {"street": "A 1", "postalCode": "8001", "locality": "Zürich"},
                   "localization": {"de": {"text": {"title": "Nice"}}}},
       "listerBranding": {"logoUrl": "https://x/logos/l_h123.png"}}
nh = {"immocode": 9, "rooms": 2, "livingArea": 50, "price": 1800, "contactCompanyName": "Livit AG"}

assert scrape.find_listings({"a": {"b": [smg, smg | {"listing": {**smg["listing"], "id": 2}}]}, "c": [1]}) == [
    smg, smg | {"listing": {**smg["listing"], "id": 2}}]
assert scrape.find_listings({"entries": [nh]}) == [nh]

h = scrape.normalize("homegate", smg)
assert (h["title"], h["rooms"], h["area_m2"], h["rent_chf"], h["city"]) == ("Nice", 3.5, 70, 2500, "Zürich")
n = scrape.normalize("newhome", nh)
assert n["provider"] == "Livit AG" and n["area_m2"] == 50

state = {**scrape.DEFAULT_STATE, "blocklist": ["livit", "l_h123"], "hidden": []}
here = state["search"]["locations"][0]["id"]
now, old = {"last_seen": "2026-02", "found_in": [here]}, {"last_seen": "2026-01", "found_in": ["geo-city-elsewhere"]}
ls = scrape.annotate({"homegate:1": h | now, "immoscout24:1": scrape.normalize("immoscout24", smg) | now,
                      "newhome:9": n | now, "newhome:8": scrape.normalize("newhome", nh | {"immocode": 8}) | old,
                      "newhome:7": scrape.normalize("newhome", nh | {"immocode": 7}) | now | {"deleted_at": "2026-02"}}, state)
assert ls["homegate:1"]["excluded"] == "blocked: l_h123"
assert ls["immoscout24:1"]["excluded"] == "duplicate of homegate"
assert ls["newhome:9"]["excluded"] == "blocked: livit"
assert ls["newhome:8"]["excluded"] == "other search" and ls["newhome:7"]["excluded"] == "deleted"
state["blocklist"] = []
assert scrape.annotate({"newhome:9": n | now}, state)["newhome:9"]["excluded"] == "rooms<3"
state["search"] = state["search"] | {"sites": ["homegate"]}
assert scrape.annotate({"newhome:9": n | now}, state)["newhome:9"]["excluded"] == "site not selected"

# your notes: merged per listing, unknown fields/statuses ignored, emptied entries dropped, attached by annotate
store, real = {}, (scrape.load, scrape.save)
scrape.load = lambda path, default: store.get(path, default)
scrape.save = lambda path, obj: store.__setitem__(path, obj)
scrape.update_notes(["homegate:1"], {"status": "shortlist", "tags": ["quiet", " quiet ", ""], "bogus": 1}, now="t1")
assert store[scrape.NOTES] == {"homegate:1": {"status": "shortlist", "tags": ["quiet"], "updated": "t1"}}
scrape.update_notes(["homegate:1", "newhome:9"], {"seen": "t2", "status": "nonsense"}, now="t3")  # seen isn't an edit
assert store[scrape.NOTES] == {"homegate:1": {"status": "shortlist", "tags": ["quiet"], "updated": "t1", "seen": "t2"},
                               "newhome:9": {"seen": "t2"}}
scrape.update_notes(["newhome:9"], {"seen": ""})
assert "newhome:9" not in store[scrape.NOTES]
assert scrape.annotate({"homegate:1": h | now}, state)["homegate:1"]["user"]["tags"] == ["quiet"]
scrape.load, scrape.save = real

# before each edit (not a mere "seen") the old notes.json is copied aside; only the newest NOTES_KEEP stay
real_notes, real_keep = scrape.NOTES, scrape.NOTES_KEEP
scrape.NOTES, scrape.NOTES_KEEP = scrape.NOTES_BACKUPS.parent / "notes.json", 2
scrape.update_notes(["homegate:1"], {"status": "maybe"})  # no notes.json yet: nothing to back up
assert not scrape.NOTES_BACKUPS.exists()
scrape.update_notes(["homegate:1"], {"seen": "t"})
assert not scrape.NOTES_BACKUPS.exists()
for status in ("shortlist", "viewing", "applied"):
    scrape.update_notes(["homegate:1"], {"status": status})
backups = sorted(scrape.NOTES_BACKUPS.glob("notes-*.json"))
assert [json.loads(b.read_text())["homegate:1"]["status"] for b in backups] == ["shortlist", "viewing"]
assert json.loads(scrape.NOTES.read_text())["homegate:1"]["status"] == "applied"
scrape.NOTES, scrape.NOTES_KEEP = real_notes, real_keep

geo = lambda id, typ: {"id": id, "names": {"de": ["Horgen"]}, "typeLabel": {"de": typ},
                       "urlNames": {"de": "ort-horgen", "en": id[4:]}, "is24UrlNames": {"en": id[4:]}}
assert scrape.location(geo("geo-region-horgen", "Bezirk"))["newhome"] == "region-horgen"
assert scrape.location(geo("geo-city-horgen", "Ort"))["newhome"] == "ort-horgen"
assert scrape.location(geo("geo-zipcode-8810", "PLZ"))["newhome"] is None
assert scrape.blocked_by({"raw": {}, "provider_label": "Livit Zürich"}, ["livit"]) == "livit"
# a company's full name also blocks its other spellings, but only as a whole word
ap = lambda name: {"raw": {"agency": {"name": name}}, "provider_label": None}
for name in ("Apleona", "Apleona Real Estate AG", "APLEONA SCHWEIZ AG", "Verwaltung: Apleona."):
    assert scrape.blocked_by(ap(name), ["Apleona Schweiz AG"]) == "Apleona Schweiz AG", name
assert scrape.blocked_by(ap("Apleonax AG"), ["Apleona Schweiz AG"]) is None
assert scrape.blocked_by(ap("Wincasa AG"), ["Apleona Schweiz AG"]) is None
assert scrape.block_core("Real Estate AG") == "real" and scrape.block_core("SA") == "sa"
assert scrape.blocked_by(ap("Livit AG"), ["Livit Real Estate Management"]) == "Livit Real Estate Management"

# AI exclusions
ex = scrape.DEFAULT_STATE["triage"]["exclude"]
ai = {"temporary": True, "temporary_until": "2027-06-30", "furnished": False, "swap": False, "shared": False,
      "kitchen": "small", "exclusions": [{"criterion": "ground floor", "matches": True, "evidence": "Erdgeschoss"},
                                           {"criterion": "no attic", "matches": False, "evidence": "photo 1: normal floor"},
                                           {"criterion": "balcony", "matches": True, "evidence": ""}],
      "evidence": {"temporary": "befristet bis 30.06.2027", "furnished": "", "swap": "", "shared": "", "kitchen": ""}}
assert scrape.ai_reasons(ai, ex) == ["temporary until 2027-06-30", "ground floor"]
assert scrape.ai_reasons(ai | {"furnished": True}, ex) == ["temporary until 2027-06-30", "ground floor"]  # no quote, ignored
assert scrape.ai_reasons(ai, ex | {"temporary_min_until": "2027-03-01", "kitchen": "small", "custom": False}) == ["small kitchen"]

import triage
# parallel triage: concurrency from TRIAGE_CONCURRENCY; a 429 makes every worker on that host wait it out
import io, os, threading, time, urllib.error, urllib.request
for v, n in ((None, 8), ("3", 3), ("0", 1), ("x", 8)):
    os.environ.pop("TRIAGE_CONCURRENCY", None) if v is None else os.environ.__setitem__("TRIAGE_CONCURRENCY", v)
    assert triage.concurrency() == n
os.environ.pop("TRIAGE_CONCURRENCY")
calls, real_urlopen = [], urllib.request.urlopen
class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass
def fake_urlopen(req, timeout):
    calls.append((json.loads(req.data)["id"], time.monotonic()))
    if calls[-1][0] == "a" and len(calls) == 1:
        raise urllib.error.HTTPError(req.full_url, 429, "", {"retry-after": "0.3"}, io.BytesIO())
    return Resp(b"{}")
urllib.request.urlopen = fake_urlopen
start = time.monotonic()
ta = threading.Thread(target=triage.post, args=("https://api.x/v1", {"id": "a"}, {})); ta.start()
time.sleep(0.05)  # b starts while a is backing off
tb = threading.Thread(target=triage.post, args=("https://api.x/v1", {"id": "b"}, {})); tb.start()
ta.join(); tb.join()
urllib.request.urlopen = real_urlopen
assert sorted(d for d, _ in calls) == ["a", "a", "b"]
assert min(t for d, t in calls if d == "b") - start >= 0.3  # b waited for a's pause
# OpenAI flex: unavailable (429, not billed) or unsupported -> the same request again on the standard tier
calls.clear()
def fake_urlopen(req, timeout):
    calls.append(json.loads(req.data))
    if "service_tier" in calls[-1]:
        raise urllib.error.HTTPError(req.full_url, 429, "", {}, io.BytesIO(b"Resource Unavailable"))
    return Resp(b"{}")
urllib.request.urlopen = fake_urlopen
assert triage.post("https://api.y/v1", {"m": 1, "service_tier": "flex"}, {}) == {} and calls == [{"m": 1, "service_tier": "flex"}, {"m": 1}]
urllib.request.urlopen = real_urlopen

# both providers: request shape + response parsing (no network)
want = {"temporary": False, "temporary_until": None, "furnished": False, "swap": False, "shared": False,
        "kitchen": "tiny", "exclusions": [], "summary": "", "available_from": None,
        "evidence": {f: "" for f in ("temporary", "furnished", "swap", "shared", "kitchen")}}
seen = []
def fake_post(url, body, headers, timeout=None):
    seen.append((url, body, headers))
    if "anthropic" in url:
        return {"content": [{"type": "text", "text": "x"}, {"type": "tool_use", "input": want}]}
    return {"choices": [{"message": {"content": json.dumps(want)}}]}
triage.post, triage.download = fake_post, lambda url: ("image/jpeg", "QUJD")
lst = {"title": "t", "rooms": 3, "area_m2": 70, "rent_chf": 2000, "address": "a", "zip": "8000", "city": "Z",
       "available_from": None, "description": "möbliert", "images": ["u1", "u2", "u3"]}
for provider in ("anthropic", "openai"):
    cfg = scrape.DEFAULT_STATE["triage"] | {"provider": provider, "max_images": 2, "custom_rules": "no {braces} issue"}
    assert triage.triage_listing(lst, cfg, provider, "k") == want
good = want
want = {"kitchen": "tiny"}  # incomplete: retried once, then an error
try:
    triage.triage_listing(lst, cfg, "openai", "k"); assert False
except RuntimeError as e:
    assert "incomplete" in str(e) and len(seen) == 4
want = good
url, body, headers = seen[0]
assert body["tools"][0]["name"] == "triage" and sum(c["type"] == "image" for c in body["messages"][0]["content"]) == 2
assert "no {braces} issue" in body["system"] and headers["x-api-key"] == "k"
url, body, headers = seen[1]
assert body["response_format"]["json_schema"]["strict"] and headers["Authorization"] == "Bearer k"
assert sum(c["type"] == "image_url" for c in body["messages"][1]["content"]) == 2

# credits run out mid-run: the run stops, finished results are kept, a rerun only sends the rest
assert triage.is_fatal(400, '{"message": "Your credit balance is too low"}') and triage.is_fatal(429, "insufficient_quota")
assert not triage.is_fatal(429, "Rate limit reached for requests") and not triage.is_fatal(500, "")
import tempfile
from pathlib import Path
real = ({n: getattr(scrape, n) for n in ("TRIAGE", "load_listings", "annotate", "write_eligible")},
        triage.triage_listing, os.environ.get("ANTHROPIC_API_KEY"))
with tempfile.TemporaryDirectory() as d:
    scrape.TRIAGE = Path(d) / "triage.json"
    L = {f"s:{i}": lst | {"excluded": None, "description": str(i)} for i in range(20)}
    scrape.load_listings, scrape.annotate, scrape.write_eligible = lambda: L, lambda l, s: l, lambda s: L
    sent, credits, lock = [], [8], threading.Lock()
    def fake_triage(l, cfg, p, key):
        sent.append(l["description"])
        with lock:
            credits[0] -= 1
            if credits[0] < 0:
                raise triage.Fatal("HTTP 400: credit balance is too low")
        return good
    triage.triage_listing, os.environ["ANTHROPIC_API_KEY"] = fake_triage, "k"
    st = {"triage": scrape.DEFAULT_STATE["triage"]}
    triage.run(st, log=lambda m: None)
    kept = set(scrape.load(scrape.TRIAGE, {}))
    assert len(kept) == 8 and len(sent) < 20  # stopped early instead of sending all 20
    sent.clear(); credits[0] = 100
    triage.run(st, log=lambda m: None)
    assert len(sent) == 12 and not {f"s:{i}" for i in map(int, sent)} & kept  # only the missing ones
    sent.clear(); triage.run(st, log=lambda m: None); assert sent == []
os.environ.pop("ANTHROPIC_API_KEY") if real[2] is None else os.environ.__setitem__("ANTHROPIC_API_KEY", real[2])
for n, v in real[0].items():
    setattr(scrape, n, v)
triage.triage_listing = real[1]

# consensus: only findings both models agree on
ev = lambda **kw: {f: kw.get(f, "") for f in ("temporary", "furnished", "swap", "shared", "kitchen")}
a = {"temporary": True, "temporary_until": "2027-03-31", "furnished": True, "swap": False, "shared": False,
     "kitchen": "tiny", "summary": "s", "available_from": None,
     "exclusions": [{"criterion": "no ground floor", "matches": True, "evidence": "EG"},
                      {"criterion": "no attic", "matches": True, "evidence": "Dachwohnung"}],
     "evidence": ev(temporary="befristet", furnished="möbliert", kitchen="photo 2")}
b = a | {"furnished": False, "temporary_until": None, "kitchen": "small", "evidence": ev(temporary="bis 31.3."),
         "exclusions": [{"criterion": "no ground floor", "matches": True, "evidence": "Hochparterre"},
                          {"criterion": "no attic", "matches": False, "evidence": ""}]}
c = scrape.consensus(a, b)
assert (c["temporary"], c["furnished"], c["kitchen"], c["temporary_until"]) == (True, False, "small", "2027-03-31")
assert c["disputed"] == ["furnished", "no attic", "kitchen tiny vs small"]
assert scrape.matched_exclusions(c) == ["no ground floor"]
dup = b | {"exclusions": [{"criterion": "no attic", "matches": True, "evidence": "title"},
                          {"criterion": "no attic", "matches": True, "evidence": "photo 3"}]}
assert scrape.matched_exclusions(scrape.consensus(a, dup)) == ["no attic"]  # repeated criterion still matches
assert scrape.consensus(a, b | {"kitchen": "unknown"})["kitchen"] == "unknown"
cfg = scrape.DEFAULT_STATE["triage"] | {"provider": "both"}
pv = scrape.AI_PROMPT_VERSION
assert scrape.ai_result({"anthropic": {"prompt": pv, "content": "old", "result": a}}, cfg | {"provider": "anthropic"}, "new") is None
assert scrape.ai_result({"anthropic": {"prompt": pv, "result": a}}, cfg) is None  # waits for both
assert scrape.ai_result({"anthropic": {"prompt": pv, "result": a}, "openai": {"prompt": pv, "result": b}}, cfg)["temporary"]
assert scrape.ai_result({"anthropic": {"prompt": pv - 1, "result": {}}}, cfg | {"provider": "anthropic"}) is None  # old prompt

# availability filter: dates outside the window exclude; "by arrangement"/unknown never do
s = scrape.DEFAULT_STATE["search"] | {"move_in_earliest": "2026-11-01", "move_in_latest": "2027-01-31"}
base = {"rooms": None, "area_m2": None, "rent_chf": None}
assert scrape.fails_search(base | {"available_from": "2027-03-01"}, s) == "available 2027-03-01"
assert scrape.fails_search(base | {"available_from": "2026-12-01"}, s) is None
assert scrape.fails_search(base | {"available_from": "immediately"}, s | {"move_in_earliest": "2999-01-01"}) == "available immediately"
assert scrape.fails_search(base | {"available_from": "immediately"}, s | {"move_in_earliest": ""}) is None
assert scrape.fails_search(base | {"available_from": "by arrangement"}, s) is None
assert scrape.fails_search(base | {"available_from": None}, s) is None
assert scrape.normalize("newhome", nh | {"availabilityType": 3})["available_from"] == "by arrangement"
assert scrape.normalize("newhome", nh | {"availabilityType": 2, "availabilityDate": "2026-12-01T00:00:00+00:00"})["available_from"] == "2026-12-01"

assert [scrape.parse_available(t) for t in ("Immediately", "By agreement", "01.12.2026", "1.3.2027", "—")] == [
    "immediately", "by arrangement", "2026-12-01", "2027-03-01", None]
assert scrape.normalize("homegate", smg | {"detail": {"available": "2026-12-01"}})["available_from"] == "2026-12-01"

# listing page beats search result; conflicts never exclude
page_listing = smg["listing"] | {"characteristics": {"numberOfRooms": 4, "livingSpace": 87}}
n = scrape.normalize("homegate", smg | {"detail": {"available": None, "listing": page_listing}})
assert (n["rooms"], n["area_m2"], n["source"]) == (4, 87, "page")
old = scrape.normalize("homegate", smg) | {"title": "Helle 2-Zimmer-Wohnung", "description": "Die Wohnung bietet auf rund 72 m² Platz"}
c = scrape.conflicts(old | {"rooms": 3.0, "area_m2": 37.0}, None)
assert c == {"rooms": "title says 2 rooms", "area_m2": "description says 72 m²"}
twin = old | {"site": "immoscout24", "rooms": 2.0, "area_m2": 34.0, "rent_chf": 3250.0, "source": "search"}
assert scrape.conflicts(old | {"rooms": 2.0, "area_m2": 70.0}, twin)["area_m2"] == "immoscout24 says 34 m²"
assert scrape.conflicts(n | {"title": "1-Zimmer"}, twin) == {}  # verified on the page
s2 = scrape.DEFAULT_STATE["search"] | {"min_area": 56, "min_rooms": 0}
assert scrape.fails_search({"rooms": 2, "area_m2": 44, "rent_chf": 2000, "available_from": None, "conflicts": {}}, s2) == "area<56"
assert scrape.fails_search({"rooms": 2, "area_m2": 44, "rent_chf": 2000, "available_from": None, "conflicts": {"area_m2": "x"}}, s2) is None

# quick / full scans and removed listings
class SearchPage:
    def __init__(self, pages): self.pages, self.n, self.url, self.closed = pages, 0, "", False
    def goto(self, url, wait_until=None): self.n += 1; self.url = url; return type("R", (), {"status": self.status(url)})()
    def status(self, url): return 404 if "/rent/gone" in url else 200
    def evaluate(self, js, arg=None):
        if js == scrape.STATE_JS:
            ids = self.pages[min(self.n, len(self.pages)) - 1]
            return json.dumps({"resultList": {"search": {"fullSearch": {"result": {"pageCount": len(self.pages),
                "listings": [{"listing": {"id": i, "prices": {}, "characteristics": {}}} for i in ids]}}}}})
        if js == scrape.DETAIL_JS: return "Immediately"
        if js == scrape.LISTING_JS: return None
    def wait_for_timeout(self, ms): pass
    def is_closed(self): return self.closed
s0 = scrape.DEFAULT_STATE["search"] | {"max_pages": 10}
loc = {"id": "L", "label": "L", "homegate": "x"}
pg = SearchPage([["n1", "k1"], ["k2", "k3"], ["k4"]]); done = {}
got = [x["listing"]["id"] for x in scrape.scrape_smg(pg, "homegate", loc, s0, lambda m: None, {"k1", "k2", "k3", "k4"}, True, done)]
assert got == ["n1", "k1", "k2", "k3"] and pg.n == 2 and not done  # quick: stops after the all-known page 2
pg = SearchPage([["n1", "k1"], ["k2", "k3"], ["k4"]]); done = {}
assert len(list(scrape.scrape_smg(pg, "homegate", loc, s0, lambda m: None, set(), False, done))) == 5 and done == {"complete": True}
listings = {"homegate:gone": {"site": "homegate", "last_seen": "1", "found_in": ["L"], "raw": {}},
            "homegate:moved": {"site": "homegate", "last_seen": "1", "found_in": ["other"], "raw": {}}}
scrape.check_removed(SearchPage([]), "homegate", ["L"], listings, "2", {}, lambda m: None)
assert listings["homegate:gone"]["deleted_at"] == "2" and "deleted_at" not in listings["homegate:moved"]

# removed listings redirect away from /rent/<id> (homegate: ...matching-list?expired=<id>)
P = lambda url: type("P", (), {"url": url})()
assert scrape.gone_from(P("https://www.homegate.ch/rent/apartment/city-au-zh/matching-list?expired=4003541825"), "4003541825")
assert not scrape.gone_from(P("https://www.homegate.ch/rent/4003541825"), "4003541825")
assert not scrape.gone_from(P("https://www.immoscout24.ch/en/rent/4003541825/"), "4003541825")
assert not scrape.gone_from(P("about:blank"), "4003541825")  # not navigated yet

# flatfox: one pin request, details only for new pks, ZIP filter, homegate duplicates
ff = {"pk": 501, "public_title": "Seestrasse 1, 8810 Horgen", "url": "/en/flat/x/501/", "number_of_rooms": "3.5",
      "livingspace": 80, "rent_gross": 2500, "zipcode": 8810, "city": "Horgen", "street": "Seestrasse 1",
      "latitude": 47.26, "longitude": 8.6, "moving_date_type": "imm", "agency": {"name": None, "logo": None},
      "images": [{"url": "/media/a.jpg"}], "smg_id": "4003545417", "description": "d"}
n = scrape.normalize("flatfox", ff)
assert (n["id"], n["rooms"], n["area_m2"], n["rent_chf"], n["zip"], n["available_from"], n["images"]) == (
    "501", 3.5, 80, 2500, "8810", "immediately", ["https://flatfox.ch/media/a.jpg"])
calls, store = [], {}
def fake_json(url, params=()):
    calls.append(url)
    if "pin" in url:  # the whole area is "full" (400 pins): split into quarters, each returns the real pins
        return [{"pk": i} for i in range(1000, 1400)] if params["east"] - params["west"] > 0.15 \
            else [{"pk": 501}, {"pk": 502}, {"pk": 400}]
    return {"results": [ff, ff | {"pk": 502, "zipcode": 6300}]}
real = scrape.get_json, scrape.load, scrape.save, scrape.area, scrape.in_location
scrape.get_json, scrape.area = fake_json, lambda loc, log: [8.5, 47.1, 8.7, 47.3]
scrape.load = lambda path, default: store.get(path, default)
scrape.save = lambda path, obj: store.__setitem__(path, obj)
scrape.in_location = lambda z, loc_id: z == "8810"
store[scrape.FLATFOX] = {"400": ff | {"pk": 400}}  # fetched before: not requested again
scrape.time.sleep = lambda t: None
done = {}
got = [x["pk"] for x in scrape.scrape_flatfox({"id": "geo-region-horgen", "label": "Horgen (Bezirk)"},
                                            scrape.DEFAULT_STATE["search"], lambda m: None, set(), False, done)]
assert got == [501, 400] and done == {"complete": True}  # 502 is in Zug (6300): outside the district
assert sum("pin" in c for c in calls) == 5  # full area + 4 quarters
scrape.get_json, scrape.load, scrape.save, scrape.area, scrape.in_location = real
st = {**scrape.DEFAULT_STATE, "blocklist": [], "hidden": []}
here = [st["search"]["locations"][0]["id"]]
ls = scrape.annotate({"flatfox:501": n | {"last_seen": "1", "found_in": here},
                      "homegate:4003545417": scrape.normalize("homegate", smg | {"listing": smg["listing"] | {"id": 4003545417}})
                      | {"last_seen": "1", "found_in": here}}, st)
assert ls["flatfox:501"]["excluded"] == "duplicate of homegate" and ls["homegate:4003545417"]["dups"] == ["flatfox:501"]

# one flat on several portals: candidates by address/coordinates + rooms/area/rent, confirmed by photos or text
desc = "Helle 3.5-Zimmerwohnung mit grosser Wohnküche, Balkon gegen Süden und Reduit, nahe Tram und Schulen."
flat = lambda site, id, **kw: {"site": site, "id": id, "rooms": 3.5, "area_m2": 80.0, "rent_chf": 2500.0,
                               "address": "Käferholzstr. 56", "lat": 47.4, "lon": 8.53, "description": desc,
                               "images": [f"{site}{id}-{i}" for i in range(3)], "url": "u", "smg_id": None} | kw
pool = {"homegate:1": flat("homegate", "1"),
        "newhome:2": flat("newhome", "2", address="Käferholzstrasse 56", rent_chf=2510.0),  # same text and address
        "flatfox:3": flat("flatfox", "3", address=None, lat=47.4005, description="anders"),  # 56 m away, same photos
        "flatfox:4": flat("flatfox", "4", address="Käferholzstr. 40", rent_chf=2600.0),  # other unit, similar text
        "newhome:5": flat("newhome", "5", rooms=4.5),  # different rooms: not even a candidate
        "homegate:6": flat("homegate", "6", address="Käferholzstr. 40")}
assert ("homegate:1", "newhome:5") not in scrape.dup_candidates(pool)
photos = {"homegate1-0": "f0f0f0f0f0f0f0f0", "homegate1-1": "0123456789abcdef", "homegate1-2": "ffff0000ffff0000",
          "flatfox3-0": "f0f0f0f0f0f0f0f1", "flatfox3-1": "0123456789abcdee",  # recompressed: a bit or two off
          "flatfox4-0": "1111111111111111", "homegate6-0": "1111111111111111",  # one shared photo of three: not enough
          "homegate6-1": "1111111111111111", "flatfox4-1": "aaaaaaaaaaaaaaaa"}
assert scrape.find_duplicates(pool, photos) == {k: ["homegate:1", "flatfox:3", "newhome:2"]
                                                for k in ("homegate:1", "flatfox:3", "newhome:2")}
photos |= {"homegate6-0": "aaaaaaaaaaaaaaaa", "homegate6-2": "cccccccccccccccc", "flatfox4-2": "cccccccccccccccc"}
assert scrape.find_duplicates(pool, photos)["flatfox:4"] == ["homegate:6", "flatfox:4"]  # its own photos match
assert scrape.street_key("Käferholzstr. 56") == scrape.street_key("Käferholzstrasse 56") == "kaferholz str 56"

# notes of all copies show on the shown one; edits go to every copy (pinned photos stay per portal)
assert scrape.merge_notes([{"status": "maybe", "tags": ["a"], "note": "x", "seen": "2", "pinned": ["p"]},
                           {"status": "viewing", "tags": ["b", "a"], "note": "y", "starred": True, "seen": "1", "pinned": ["q"]}, {}]) == {
    "status": "viewing", "tags": ["a", "b"], "note": "x\n\ny", "starred": True, "seen": "1", "pinned": ["p"]}
assert scrape.merge_notes([{}, {"pinned": ["q"]}]) == {}
store, real = {}, (scrape.load, scrape.save)
scrape.load = lambda path, default: store.get(path, default)
scrape.save = lambda path, obj: store.__setitem__(path, obj)
scrape._clusters = {"homegate:1": ["homegate:1", "newhome:2"], "newhome:2": ["homegate:1", "newhome:2"]}
scrape.update_notes(["homegate:1"], {"status": "maybe", "pinned": ["p"]}, now="t")
assert store[scrape.NOTES] == {"homegate:1": {"status": "maybe", "pinned": ["p"], "updated": "t"},
                               "newhome:2": {"status": "maybe", "updated": "t"}}
listed = lambda: {k: l | {"last_seen": "1", "found_in": here, "source": "page", "zip": "8050", "city": "Zürich",
                          "title": "t", "available_from": None, "provider": None, "provider_logo": None, "raw": {}}
                  for k, l in pool.items()}
ls = scrape.annotate(listed(), {**st, "hidden": ["newhome:2"]})  # hidden on newhome, but given a status: kept
assert not ls["homegate:1"]["excluded"] and ls["newhome:2"]["dup_of"] == "homegate:1"
assert ls["homegate:1"]["user"]["status"] == "maybe" and [a["site"] for a in ls["homegate:1"]["also_on"]] == ["newhome"]
store[scrape.NOTES] = {}
assert scrape.annotate(listed(), {**st, "hidden": ["newhome:2"]})["homegate:1"]["excluded"] == "hidden"
scrape.load, scrape.save, scrape._clusters = *real, {}

# rate limiting: back off 30s, 60s on 429/503, then go on
class FakePage:
    def __init__(self, statuses): self.statuses, self.waits = list(statuses), []
    def goto(self, url, wait_until=None): return type("R", (), {"status": self.statuses.pop(0)})()
    def wait_for_timeout(self, ms): self.waits.append(ms)
fp = FakePage([429, 503, 200])
assert scrape.goto(fp, "u", lambda m: None).status == 200
assert 24_000 <= fp.waits[0] <= 36_000 and 48_000 <= fp.waits[1] <= 72_000  # 30s, 60s +-20% jitter

# commute: alternative addresses, typed transfers, night return; missing data never excludes
rail, tram, bus = ({"type": t, "line": n} for t, n in (("COMMUTER_TRAIN", "S7"), ("TRAM", "4"), ("BUS", "912")))
pt = {"name": "City", "addresses": "Zürich HB\nOpernhaus Zürich", "mode": "transit", "arrive_by": "08:30",
      "max_minutes": 40, "max_km": 0, "max_transfers": 0, "free_train_transfers": True, "return_at": "00:30",
      "max_night_minutes": 60}
lst = {"lat": 47.3, "lon": 8.6}
routes = {scrape.commute_key(lst, pt, "Zürich HB"): [{"min": 25, "km": 15, "rides": [bus, rail]}],          # 1 transfer
          scrape.commute_key(lst, pt, "Opernhaus Zürich"): [{"min": 32, "km": 14, "rides": [tram]}],        # direct tram
          scrape.commute_key(lst, pt, "Zürich HB", back=True): [{"min": 30, "km": 15, "rides": [rail, bus], "wait_min": 380}],
          scrape.commute_key(lst, pt, "Opernhaus Zürich", back=True): [{"min": 30, "km": 14, "rides": [tram], "wait_min": 45}]}
r = scrape.commute_result(lst, pt, routes)
assert (r["to"], r["min"], r["transfers"], r["night_min"]) == ("Opernhaus Zürich", 32, 0, 45)  # tram beats HB + bus change
assert scrape.transfers({"rides": [rail, rail, tram]}, True) == 1 and scrape.transfers({"rides": [rail, rail]}, False) == 1
assert scrape.commute_reasons({"commute": [r]}, [pt]) == []
assert scrape.commute_reasons({"commute": [r | {"night_min": 380}]}, [pt]) == ["City night return 380 min"]
assert scrape.commute_reasons({"commute": [r | {"min": 50, "night_min": None}]}, [pt]) == ["City 50 min"]
assert scrape.commute_reasons({"commute": ["no route", None]}, [pt, pt]) == []  # no data: keep
assert scrape.commute_result(lst, pt, {}) is None  # not computed yet
assert scrape.addresses({"address": "Old single"}) == ["Old single"]  # old state format

# Google route parsing: rides from transit steps, night wait = last arrival + trailing walk - requested departure
import commute
from datetime import datetime, timezone
rt = {"duration": "1980s", "distanceMeters": 14200, "legs": [{"steps": [
    {"travelMode": "WALK", "staticDuration": "300s"},
    {"travelMode": "TRANSIT", "transitDetails": {"stopDetails": {"arrivalTime": "2026-10-14T00:20:00Z"},
                                                 "transitLine": {"nameShort": "4", "vehicle": {"type": "TRAM"}}}},
    {"travelMode": "WALK", "staticDuration": "240s"}]}]}
r = commute.parse(rt, depart_at=datetime(2026, 10, 13, 22, 30, tzinfo=timezone.utc))
assert r == {"min": 33, "km": 14.2, "rides": [{"type": "TRAM", "line": "4"}], "wait_min": 114}
assert commute.weekday_at("00:30", night=True).weekday() == 2 and commute.weekday_at("08:30").weekday() == 1

# tax baseline: cheapest commune among current listings (hidden counts, stale doesn't)
cache = {json.dumps([{"income": 1}, z], sort_keys=True): {"tax": t, "commune": z} for z, t in [("8000", 24000), ("6300", 12000), ("1000", 6000)]}
scrape.load = lambda path, default: cache
ls = {"zh": {"zip": "8000", "rent_chf": 2000, "excluded": None}, "zg": {"zip": "6300", "rent_chf": 2500, "excluded": "hidden"},
      "old": {"zip": "1000", "rent_chf": 900, "excluded": "deleted"}}
scrape.add_taxes(ls, {"income": 1})
assert (ls["zg"]["tax_extra_month_chf"], ls["zg"]["rent_tax_adjusted_chf"]) == (0, 2500)
assert (ls["zh"]["tax_extra_month_chf"], ls["zh"]["rent_tax_adjusted_chf"]) == (1000, 3000)

# map exclusion areas: polygon test, switched-off and degenerate areas ignored, missing coordinates never excluded
sq = [[47.0, 8.0], [47.0, 8.2], [47.2, 8.2], [47.2, 8.0]]
tri = [[47.0, 8.0], [47.2, 8.2], [47.2, 8.0]]  # the half north-west of the diagonal
assert scrape.in_polygon(47.1, 8.1, sq) and not scrape.in_polygon(47.3, 8.1, sq)
assert scrape.in_polygon(47.15, 8.05, tri) and not scrape.in_polygon(47.05, 8.15, tri)
areas = [{"name": "off", "on": False, "points": sq}, {"name": "line", "points": sq[:2]}, {"name": "Lake", "points": tri}]
assert scrape.excluding_area({"lat": 47.15, "lon": 8.05}, areas)["name"] == "Lake"
assert scrape.excluding_area({"lat": 47.05, "lon": 8.15}, areas) is None
assert scrape.excluding_area({"lat": None, "lon": None}, areas) is None
st = {**scrape.DEFAULT_STATE, "blocklist": [], "exclusion_areas": areas}
assert scrape.annotate({"homegate:1": h | now | {"lat": 47.15, "lon": 8.05}}, st)["homegate:1"]["excluded"] == "area: Lake"

# deploy/.env: comments, quotes, export prefix; the real environment wins
import os, tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as d:
    env = Path(d) / ".env"
    env.write_text('# c\nexport T_A="x y"\nT_B=\'z\'\nT_C = 1=2\nT_D=file\n\nnot a pair\n')
    os.environ["T_D"] = "shell"
    scrape.load_env(env)
    assert [os.environ.pop(k) for k in ("T_A", "T_B", "T_C", "T_D")] == ["x y", "z", "1=2", "shell"]
    scrape.load_env(Path(d) / "missing")

import publish

shown = {"a": {}, "b": {"user": {"status": "maybe"}}, "c": {"excluded": "deleted", "user": {"status": "shortlist"}},
         "d": {"excluded": "hidden", "user": {"status": "shortlist"}}, "e": {"user": {"status": "applied", "starred": True}}}
assert list(publish.published(shown)) == ["b", "c"]
page = publish.view_only((Path(publish.__file__).parent / "index.html").read_text(), "body{}", "x()",
                         {"listings": {}, "note": "</script>"})
assert '<button id="scrape" hidden' in page and 'data-tab="settings"' not in page and "<fieldset>" not in page
assert "/api/export" not in page and "geoman" not in page and 'id="invite"' not in page and '<button id="inviteBtn" hidden' in page
assert '<html lang="en" class="static">' in page and "<\\/script>" in page and "/static/app" not in page
pub = publish.public_state({**scrape.DEFAULT_STATE, "tax": {**scrape.DEFAULT_STATE["tax"], "income": 150000},
                            "commute": {"points": [{"name": "Work", "mode": "transit", "addresses": "Secret 1", "return_at": ""}]}})
assert pub == {"tax": {"income": 1}, "commute": {"points": [{"name": "Work", "mode": "transit", "return_at": ""}]}}

print("ok")

# floor: the portal's field in German convention, else the description's; the description flags a portal typo
fl = lambda site, raw, desc=None, title=None: {k: v for k, v in scrape.listing_floor(
    site, {"raw": raw, "title": title, "description": desc}).items() if k in ("floor", "floor_source", "floor_n")}
assert fl("flatfox", {"floor": 0}) == {"floor": "EG", "floor_source": None, "floor_n": 0}
assert fl("flatfox", {"floor": 3})["floor"] == "3. Stock" and fl("flatfox", {"floor": -1})["floor"] == "UG"
assert fl("newhome", {"floor": 2})["floor"] == "EG" and fl("newhome", {"floor": 9})["floor"] == "7. Stock"
assert fl("newhome", {"floor": 21})["floor"] == "9. Stock" and fl("newhome", {"floor": 13})["floor"] == "Hochparterre"
assert fl("homegate", {"listing": {"characteristics": {"floor": 0, "isGroundFloorRaised": True}}})["floor"] == "Hochparterre"
assert fl("homegate", smg)["floor"] is None
assert fl("flatfox", {"floor": None}, "Schöne 3.5-Zimmer-Wohnung im 2. Obergeschoss") == {
    "floor": "2. Stock", "floor_source": "text", "floor_n": 2}
assert fl("flatfox", {}, "The apartment is located on the 5th floor")["floor"] == "5. Stock"
assert fl("flatfox", {}, "appartement de 3 pièces au rez-de-chaussée")["floor"] == "EG"
assert fl("flatfox", {}, "Kellerabteil pro Wohnung im Untergeschoss. Waschküche im UG.")["floor"] is None
assert fl("flatfox", {}, "Die Fotos zeigen eine identische Wohnung im 1. OG.")["floor"] is None
assert fl("flatfox", {}, "Thai Take-Away im Erdgeschoss.")["floor"] is None
assert fl("flatfox", {"floor": -4}, "3-Zimmerwohnung im 4. Obergeschoss")["floor"] == "4. Stock"  # portal typo
typo = scrape.normalize("flatfox", {"pk": 1, "floor": 5, "description": "Die Wohnung im 2. Stock bietet 53 m²"})
assert typo["floor"] == "5. Stock" and scrape.conflicts(typo, None)["floor"] == "description says 2. Stock"
attic = scrape.normalize("flatfox", {"pk": 1, "floor": 4, "description": "Attikawohnung mit Terrasse"})
assert "floor" not in scrape.conflicts(attic, None)
