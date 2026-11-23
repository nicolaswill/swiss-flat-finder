"""AI triage of listings (description + photos) via Anthropic or OpenAI.

    python triage.py      # triage eligible listings that have no up-to-date result yet

Results are cached per listing in data/triage.json; scrape.annotate() turns flags into exclusions.
"""
import base64
import hashlib
import itertools
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

import scrape

SECRETS = scrape.DATA / "secrets.json"
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-6-luna"}  # luna: ~1/40 of gpt-5.6-sol per token
ENV_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
PROMPT_VERSION = scrape.AI_PROMPT_VERSION
DEFAULT_CONCURRENCY = 8  # parallel listing requests; override with TRIAGE_CONCURRENCY

SYSTEM = """You triage Swiss rental apartment listings for someone looking for a long-term, unfurnished \
apartment to live in. Listings are mostly German, sometimes French, Italian or English. You get the listing \
text and some photos (which may include floor plans, exterior shots or staged/stock images).

Judge each criterion from the evidence only. Set a flag to true only with clear evidence; when unclear, use \
false (or "unknown" for the kitchen). For every true flag, put a short verbatim quote from the listing text \
(or "photo N: <what it shows>") into the matching `evidence` field; leave the evidence of false flags empty. \
A true flag without evidence of that specific criterion is a mistake: e.g. an attic or a small kitchen is no \
evidence of a temporary lease.

Criteria:
- temporary: the lease is limited in time, e.g. "befristet", "befristeter Mietvertrag", "Zwischenmiete", \
"Untermiete für 6 Monate", "bis 31.03.2027", "Wohnen auf Zeit", "Zwischennutzung bis Abbruch". NOT temporary: \
a start date ("per 1.12.", "ab sofort", "nach Vereinbarung"), a minimum lease term, notice periods.
- temporary_until: the lease end date as YYYY-MM-DD if stated (use the last day of a stated month), else null.
- furnished: rented fully or partly furnished: "möbliert", "teilmöbliert", "furnished", "meublé", serviced or \
business apartment, furniture explicitly included. NOT furnished: built-in wardrobes, kitchen appliances, \
furniture that only appears in photos without the text saying it is included (home staging).
- swap: a "Tauschwohnung" / "Wohnungstausch": the tenant only gives up the apartment in exchange for another one.
- shared: a room in a shared flat (WG-Zimmer, Mitbewohner gesucht) instead of the whole apartment.
- kitchen: kitchen size, from photos and wording such as "Kochnische", "Kleinküche", "grosse Wohnküche". \
Real-estate photos use wide-angle lenses, so measure instead of judging the feel: use the 60 cm base-cabinet \
modules (sink, hob and dishwasher each take one) as the scale, and look at two things: the FREE worktop (counter \
not taken by sink, drainer or hob) and the floor space in front of the counters.
  tiny = a Kochnische / kitchenette: essentially just sink and hob (often a 2-zone hob) on one short run, with \
about 1 m of free counter or less, in a niche, alcove or narrow strip of a room. Wall cabinets above, an oven \
under the hob, a dishwasher or a narrow ledge continuing along the window don't change that. Not tiny: a \
full-height appliance column (fridge/oven tower) plus counters on a second wall, or a kitchen open to the living \
area with space in front of it;
  small = a closed kitchen room or galley with counters on one or two walls but a narrow aisle and only about \
1.5 m of free counter or less in total, as in many 1960s–90s Swiss flats; an L or U made of short runs is small;
  normal = comfortable free counter (clearly more than 1.5 m in one or two stretches) and room to move, including \
kitchens open to the living/dining area;
  large = long runs on two or more sides with plenty of free worktop, an island or peninsula, or a kitchen with \
its own dining area.
  unknown = no photo clearly showing the kitchen and no textual hint.
  If the listing says its photos are illustrative ("nicht objektspezifisch", "Symbolbilder", "Beispielbilder"), they \
usually show another unit of the same building: still rate the kitchen from them, and add "(illustrative photos)" \
to the kitchen evidence.
- exclusions: the user's own exclusion criteria are listed at the end, one per line. Each line describes an \
UNWANTED kind of listing (e.g. "attic apartment", "ground floor", "no balcony"). Return exactly one entry per \
line, in the same order, with the line copied verbatim as `criterion`. matches = true only if this listing \
clearly IS such an unwanted listing, with a quote or "photo N: ..." as evidence. matches = false with empty \
evidence if the listing is not like that or it is unclear. Be conservative: a false match hides a good \
apartment from the user, a missed match only costs them a look. Judge the apartment itself, not the \
building: e.g. for "attic apartment", match only if the text says so (Dachwohnung, Dachgeschoss, unter dem \
Dach, Mansarde...) or interior photos clearly show sloped ceilings/walls (Dachschrägen) inside the rooms; a \
top floor, a roof overhang seen outside a window, or a pitched roof on an exterior shot is not enough. \
Text only counts if it explicitly describes THIS unit (its title, or e.g. "diese Wohnung liegt im \
Dachgeschoss"). Listings for new builds or renovations often describe several units: if the text only says \
that some units in the building are like that (e.g. "in the attic apartments..."), don't infer that this unit \
is one of them; decide from this unit's interior photos alone. Empty list if there are no criteria.
- available_from: move-in date of this unit if stated: YYYY-MM-DD ("per 1. Dezember" -> the next 1 December; \
a month alone -> its first day), "immediately" (ab sofort, per sofort), "by arrangement" (nach Vereinbarung), \
else null.
- summary: one short English sentence on anything notable for the apartment hunter (don't repeat rooms, \
size or price).

User's exclusion criteria (each line = a kind of listing to exclude):
{rules}"""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["temporary", "temporary_until", "furnished", "swap", "shared", "kitchen",
                 "exclusions", "evidence", "available_from", "summary"],
    "properties": {
        "temporary": {"type": "boolean"},
        "temporary_until": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "furnished": {"type": "boolean"},
        "swap": {"type": "boolean"},
        "shared": {"type": "boolean"},
        "kitchen": {"type": "string", "enum": ["tiny", "small", "normal", "large", "unknown"]},
        "exclusions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["criterion", "matches", "evidence"],
            "properties": {"criterion": {"type": "string"}, "matches": {"type": "boolean"}, "evidence": {"type": "string"}},
        }},
        "evidence": {  # per flag, so a flag can't hide behind another flag's quote
            "type": "object", "additionalProperties": False,
            "required": ["temporary", "furnished", "swap", "shared", "kitchen"],
            "properties": {k: {"type": "string"} for k in ["temporary", "furnished", "swap", "shared", "kitchen"]},
        },
        "available_from": {"type": ["string", "null"],
                           "description": "YYYY-MM-DD, 'immediately', 'by arrangement', or null"},
        "summary": {"type": "string"},
    },
}


def api_key(provider):
    return os.environ.get(ENV_KEYS[provider]) or scrape.load(SECRETS, {}).get(provider)


def list_models(provider):
    """Chat models the stored key can use (newest first), for the UI's model picker; [] without a key."""
    if not (key := api_key(provider)):
        return []
    if provider == "anthropic":
        req = urllib.request.Request("https://api.anthropic.com/v1/models?limit=100",
                                     headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return [m["id"] for m in json.load(r)["data"]]  # already newest first
    req = urllib.request.Request("https://api.openai.com/v1/models", headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=15) as r:
        ids = [m["id"] for m in json.load(r)["data"]]
    skip = ("codex", "search", "audio", "realtime", "transcribe", "tts", "image", "chat-latest", "-pro")
    keep = [i for i in ids if i.startswith(("gpt-4.1", "gpt-5", "gpt-6", "o3", "o4"))
            and not any(x in i for x in skip) and not re.search(r"-\d{4}-\d{2}-\d{2}$", i)]  # no dated aliases
    return sorted(keep, reverse=True, key=lambda i: (not i.startswith("o"), i))  # gpt-* first, newest first


def save_key(provider, key):
    secrets = scrape.load(SECRETS, {}) | {provider: key.strip()}
    scrape.save(SECRETS, secrets)
    SECRETS.chmod(0o600)


def model_of(cfg, provider):
    return cfg["models"][provider].strip() or DEFAULT_MODELS[provider]


def version(cfg, provider):
    """Results from another prompt/model/rules set are re-triaged (cached per provider)."""
    v = [PROMPT_VERSION, provider, model_of(cfg, provider), cfg["custom_rules"].strip(), cfg["max_images"]]
    return hashlib.sha1(json.dumps(v).encode()).hexdigest()[:12]


def concurrency():
    try:
        return max(1, int(os.environ.get("TRIAGE_CONCURRENCY") or DEFAULT_CONCURRENCY))
    except ValueError:
        return DEFAULT_CONCURRENCY


# host -> monotonic time until which nobody sends: one 429 pauses all workers, not just the one that got it
_paused_until, _pause_lock = {}, threading.Lock()


def _wait_for(host):
    while (delay := _paused_until.get(host, 0) - time.monotonic()) > 0:
        time.sleep(delay)


def _pause(host, seconds):
    with _pause_lock:
        _paused_until[host] = max(_paused_until.get(host, 0), time.monotonic() + seconds)


class Fatal(RuntimeError):
    """An error every further request would hit too (no credits, bad key): the run stops instead of burning through the queue."""


def is_fatal(code, msg):
    return code in (401, 402, 403) or re.search(r"credit|quota|billing", msg, re.I) is not None


def post(url, body, headers, timeout=120):
    """POST JSON; on rate limiting / overload, back off exponentially with jitter (or as told by retry-after)."""
    host = urlsplit(url).hostname
    for attempt in range(5):
        req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
        _wait_for(host)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if body.get("service_tier") == "flex" and (e.code == 429 or "service_tier" in msg):
                # flex capacity unavailable (not billed) or flex unsupported by this model: go standard
                body = {k: v for k, v in body.items() if k != "service_tier"}
                continue
            if is_fatal(e.code, msg):  # e.g. Anthropic 400 "credit balance is too low", OpenAI 429 insufficient_quota
                raise Fatal(f"HTTP {e.code}: {msg[:300]}") from None
            if e.code not in (429, 500, 502, 503, 529) or attempt == 4:
                raise RuntimeError(f"HTTP {e.code}: {msg[:300]}") from None
            # jitter keeps the parallel workers from retrying in lockstep
            delay = float(e.headers.get("retry-after") or 2 ** attempt) * random.uniform(1, 1.5)
            if e.code in (429, 529):  # rate limited / overloaded: hold every worker sending to this host
                _pause(host, delay)
            time.sleep(delay)
    raise RuntimeError("no answer after retries")


def download(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
            mime = r.headers.get_content_type()
            return (mime, base64.b64encode(r.read()).decode()) if mime.startswith("image/") else None
    except Exception:
        return None  # a missing photo shouldn't block the triage


def listing_text(l):
    facts = {k: l[k] for k in ("title", "rooms", "area_m2", "rent_chf", "address", "zip", "city", "available_from")}
    return f"Listing facts: {json.dumps(facts, ensure_ascii=False)}\n\nDescription:\n{(l['description'] or '(none)')[:8000]}"


def triage_listing(l, cfg, provider, key):
    """One model's verdict; retried once if the answer misses fields (Claude's tool input isn't schema-enforced)."""
    for attempt in range(2):
        r = ask(l, cfg, provider, key)
        if not (missing := set(SCHEMA["required"]) - r.keys()) and set(SCHEMA["properties"]["evidence"]["required"]) <= r["evidence"].keys():
            return r
    raise RuntimeError(f"incomplete answer, missing {sorted(missing) or 'evidence fields'}")


def ask(l, cfg, provider, key):
    urls = l["images"][:cfg["max_images"]]
    images = [i for i in map(download, urls) if i]
    system = SYSTEM.format(rules=cfg["custom_rules"].strip() or "(none)")
    text = listing_text(l) + f"\n\n{len(images)} photo(s) follow, numbered from 1. Answer by calling the triage tool."
    if provider == "anthropic":
        content = [{"type": "text", "text": text}] + [
            {"type": "image", "source": {"type": "base64", "media_type": m, "data": d}} for m, d in images]
        r = post("https://api.anthropic.com/v1/messages", {
            "model": model_of(cfg, provider), "system": system,
            "max_tokens": 4096,  # newer models think before answering; 1024 truncated the tool call
            "tools": [{"name": "triage", "description": "Report the listing triage", "input_schema": SCHEMA}],
            "tool_choice": {"type": "auto"},  # newer models reject forced tool use; the prompt asks for the tool
            "messages": [{"role": "user", "content": content}],
        }, {"x-api-key": key, "anthropic-version": "2023-06-01"})
        if call := next((c["input"] for c in r["content"] if c["type"] == "tool_use"), None):
            return call
        raise RuntimeError(f"model answered without the triage tool: {str(r['content'])[:200]}")
    content = [{"type": "text", "text": text}] + [
        {"type": "image_url", "image_url": {"url": f"data:{m};base64,{d}", "detail": "high"}} for m, d in images]
    r = post("https://api.openai.com/v1/chat/completions", {
        "model": model_of(cfg, provider),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "triage", "strict": True, "schema": SCHEMA}},
        "service_tier": "flex",  # half price for slower answers; post() falls back to standard when flex is unavailable
    }, {"Authorization": f"Bearer {key}"}, timeout=900)
    return json.loads(r["choices"][0]["message"]["content"])


def summarize(r):
    flags = [f for f in scrape.FLAGS if scrape.flagged(r, f)] + scrape.matched_exclusions(r)
    return f"kitchen {r['kitchen']}" + (", " + ", ".join(flags) if flags else "")


def run(state, log=print):
    cfg = state["triage"]
    providers = scrape.ai_providers(cfg)
    keys = {p: api_key(p) for p in providers}
    if missing := [p for p in providers if not keys[p]]:
        log(f"AI triage: no API key for {', '.join(missing)}; add it in the UI (or set {ENV_KEYS[missing[0]]})")
        return
    results = {k: v for k, v in scrape.load(scrape.TRIAGE, {}).items() if "v" not in v}  # drop pre-v2 format
    listings = scrape.annotate(scrape.load_listings(), state)
    # Only listings that are otherwise eligible: no point paying to triage blocked/duplicate/stale ones.
    todo = [(k, l, p) for k, l in listings.items() if not l["excluded"] or l["excluded"].startswith("AI:")
            for p in providers if (e := results.get(k, {}).get(p, {})).get("v") != version(cfg, p)
            or e.get("content") != scrape.content_hash(l)]
    workers = concurrency()
    log(f"AI triage: {len(todo)} request(s), {workers} at a time, with "
        + ", ".join(f"{p}/{model_of(cfg, p)}" for p in providers))
    done = itertools.count(1)
    stop = threading.Event()  # set on a Fatal error: queued listings are skipped, not sent

    def one(task):
        k, l, p = task
        if stop.is_set():
            return task, None
        try:
            r = triage_listing(l, cfg, p, keys[p])
            log(f"  {next(done)}/{len(todo)} {k} [{p}]: {summarize(r)}")
            return task, r
        except Exception as e:
            if isinstance(e, Fatal) and not stop.is_set():
                stop.set()
                log(f"AI triage stopping: {e}")
            log(f"  {next(done)}/{len(todo)} {k} [{p}] failed: {e}")
            return task, None

    # One request per provider first: a bad key/model fails once with a clear message instead of N times.
    firsts = [next(t for t in todo if t[2] == p) for p in providers if any(t[2] == p for t in todo)]
    with ThreadPoolExecutor(max(1, len(firsts))) as ex:  # with "both", the two checks run side by side
        checked = list(ex.map(one, firsts))
    if any(r is None for _, r in checked):
        log("AI triage stopped: fix the error above (API key, model name or credits) and retry")
        return
    rest = [t for t in todo if t not in firsts]
    ok = 0
    with ThreadPoolExecutor(workers) as ex:
        # as_completed: one slow listing doesn't hold back saving the ones that finished after it
        finished = (f.result() for f in as_completed([ex.submit(one, t) for t in rest]))
        for (k, l, p), r in itertools.chain(checked, finished):
            if r:
                ok += 1
                results.setdefault(k, {})[p] = {"v": version(cfg, p), "prompt": PROMPT_VERSION,
                                                "content": scrape.content_hash(l), "result": r}
                # saved on every result: a crash or stop loses nothing paid for, and the UI shows results live
                scrape.save(scrape.TRIAGE, results)
    eligible = sum(not l["excluded"] for l in scrape.write_eligible(scrape.load_state()).values())
    log(f"AI triage {'stopped' if stop.is_set() else 'done'}: {ok} ok, {len(todo) - ok} failed or skipped, "
        f"{eligible} eligible" + (" (a rerun triages only those)" if ok < len(todo) else ""))


if __name__ == "__main__":
    run(scrape.load_state())
