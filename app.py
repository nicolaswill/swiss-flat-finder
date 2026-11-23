"""Local review UI: python app.py, then open http://localhost:8765"""
import json
import threading
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import scrape
import commute
import dedup
import pdf
import publish
import triage

ROOT = Path(__file__).parent
run = {"running": False, "log": []}
state_lock = threading.Lock()
notes_lock = threading.Lock()
STATIC = {"/index.html", "/static/app.js", "/static/app.css", "/export.html", "/static/export.js"}
# Everything the annotated listings are computed from, except your notes and hidden listings (scrape.overlay):
# while none of these and the rest of the settings change, /api/data reuses the annotated listings.
INPUTS = (scrape.LISTINGS, scrape.TRIAGE, scrape.COMMUTE, scrape.LOGOS, scrape.TAXES, scrape.PHOTOS)
data_cache = {"sig": None, "base": {}, "full_sig": None, "listings": {}}
data_lock = threading.Lock()
mtime = lambda p: (st.st_mtime_ns, st.st_size) if (st := p.stat() if p.exists() else None) else None


def listings_data():
    """The annotated listings (without raw site data), plus a version that changes whenever they may have.
    A status, note or hide only redoes the cheap overlay, not the exclusions, AI, commute and taxes."""
    state = scrape.load_state()
    settings = {k: v for k, v in state.items() if k not in ("hidden", "rev")}
    sig = (tuple(mtime(p) for p in INPUTS), json.dumps(settings, sort_keys=True))
    with data_lock:  # one computation at a time: polls arriving together wait for it instead of repeating it
        if data_cache["sig"] != sig:
            base = scrape.annotate_base(scrape.load_listings(), state)
            for l in base.values():
                l.pop("raw")
            data_cache.update(sig=sig, base=base, full_sig=None)
        full_sig = (sig, mtime(scrape.NOTES), tuple(state["hidden"]))
        if data_cache["full_sig"] != full_sig:
            listings = scrape.overlay({k: dict(l) for k, l in data_cache["base"].items()},
                                      state["hidden"], scrape.load(scrape.NOTES, {}))
            data_cache.update(full_sig=full_sig, version=str(hash(full_sig)), listings=listings)
        return data_cache["listings"], data_cache["version"]


# data/eligible.json is only an output file: written a moment after the last save, so a burst of hides
# recomputes it once, and a save returns at once
eligible_timer = None


def schedule_eligible():
    global eligible_timer
    if eligible_timer:
        eligible_timer.cancel()
    eligible_timer = threading.Timer(2, lambda: scrape.write_eligible(scrape.load_state()))
    eligible_timer.daemon = True
    eligible_timer.start()


def do_scrape(quick=False, sites=None):
    scrape.scrape(state := scrape.load_state(), log=run["log"].append, quick=quick, sites=sites)
    dedup.run(scrape.load_state(), log=run["log"].append)  # first: commute and triage skip the duplicates
    if state["commute"]["auto"]:  # before triage, which then skips commute-excluded listings
        commute.run(scrape.load_state(), log=run["log"].append)
    if state["triage"]["auto"]:
        triage.run(scrape.load_state(), log=run["log"].append)


def start(job):
    """Run one background job at a time; its log is polled by the UI via /api/data."""
    def wrapped():
        try:
            job()
        except Exception as e:
            run["log"].append(f"error: {e!r}")
        finally:
            run["running"] = False

    if not run["running"]:
        run.update(running=True, log=[])
        threading.Thread(target=wrapped, daemon=True).start()


class Handler(SimpleHTTPRequestHandler):
    def send_json(self, obj, download=None, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if download:
            self.send_header("Content-Disposition", f"attachment; filename={download}")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/data":
            listings, version = listings_data()
            keys = {p: bool(triage.api_key(p)) for p in triage.ENV_KEYS} | {"google_maps": bool(commute.api_key())}
            return self.send_json({"state": scrape.load_state(), "version": version, "run": run, "keys": keys,
                                   "default_models": triage.DEFAULT_MODELS, "listings": listings})
        if self.path == "/api/run":  # polled while a job runs: just the log, not the listings
            return self.send_json({"run": run})
        if self.path == "/api/export":
            return self.send_json(scrape.load(scrape.ELIGIBLE, []), download="eligible.json")
        if self.path == "/api/pdf":  # maybe + shortlist listings with photos, notes and maps
            try:
                body = pdf.render(f"http://127.0.0.1:{self.server.server_address[1]}/export.html?pdf",
                                  listings_data()[0])
            except Exception as e:
                return self.send_json({"error": f"{e!r}"}, status=500)
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f"attachment; filename=flats-{date.today()}.pdf")
            self.end_headers()
            return self.wfile.write(body)
        if self.path == "/api/models":
            out = {}
            for p in triage.ENV_KEYS:
                try:
                    out[p] = triage.list_models(p)
                except Exception:  # bad key / offline: the picker still offers the default
                    out[p] = []
            return self.send_json(out)
        if self.path.startswith("/api/locations?"):
            q = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
            return self.send_json(scrape.find_locations(q) if q.strip() else [])
        if self.path == "/":
            self.path = "/index.html"
        if urlsplit(self.path).path not in STATIC:  # export.html?pdf
            return self.send_error(404)
        return super().do_GET()

    def do_POST(self):
        # JSON content type forces a CORS preflight, so other websites can't POST here (e.g. to spend API credits).
        if self.headers.get("Content-Type") != "application/json":
            return self.send_error(415)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or "null")
        if self.path == "/api/state":
            with state_lock:  # a tab saving settings it loaded before another tab's save must not overwrite them
                if body.get("rev", 0) != (rev := scrape.load_state()["rev"]):
                    return self.send_json({"error": "stale"}, status=409)
                body["rev"] = rev + 1
                body["hidden"] = scrape.load_state()["hidden"]  # only /api/hidden changes these
                scrape.save(scrape.STATE, body)
            schedule_eligible()
            return self.send_json({"ok": True, "rev": body["rev"]})
        elif self.path == "/api/hidden":  # hide (reject) or unhide listings
            with state_lock:
                scrape.set_hidden(body["keys"], bool(body["hidden"]))
            schedule_eligible()
            return self.send_json({"ok": True})
        elif self.path == "/api/notes":  # your status/star/tags/note/pinned photos for one or more listings
            with notes_lock:
                return self.send_json(scrape.update_notes(body["keys"], body["patch"]))
        elif self.path == "/api/secrets":
            triage.save_key(body["provider"], body["key"])
        elif self.path == "/api/scrape":
            start(lambda: do_scrape(quick=bool(body.get("quick")), sites=body.get("sites")))
        elif self.path == "/api/refresh":  # re-read specific listings from their own pages
            start(lambda: (scrape.refresh(body["keys"], log=run["log"].append),
                           scrape.write_eligible(scrape.load_state())))
        elif self.path == "/api/commute":
            start(lambda: commute.run(scrape.load_state(), log=run["log"].append))
        elif self.path == "/api/triage":
            start(lambda: triage.run(scrape.load_state(), log=run["log"].append))
        elif self.path == "/api/invite":  # an invitation link to the published page (needs az, signed in)
            try:
                return self.send_json(publish.invite(body["who"], log=run["log"].append))
            except Exception as e:
                return self.send_json({"error": str(e)}, status=400 if isinstance(e, ValueError) else 500)
        elif self.path == "/api/publish":  # results as a read-only static site on Azure (publish.py)
            start(lambda: publish.run(log=run["log"].append))
        else:
            return self.send_error(404)
        self.send_json({"ok": True})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("http://localhost:8765")
    start(lambda: dedup.run(scrape.load_state(), log=run["log"].append))  # photos of listings scraped before
    ThreadingHTTPServer(("127.0.0.1", 8765), lambda *a: Handler(*a, directory=str(ROOT))).serve_forever()
