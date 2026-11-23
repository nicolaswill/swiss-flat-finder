"""Commute times from each listing to the user's destinations via the Google Maps Routes API.

    python commute.py     # compute missing commutes for otherwise-eligible listings

A destination can have several addresses (e.g. Zürich HB, Opernhaus, Bellevue): the best route to any of them
counts. Raw route alternatives (lines ridden, vehicle types) are cached in data/commute.json, so transfer rules
can change without new requests; scrape.annotate() picks the best route and turns limit violations into exclusions.
"""
import os
import time as time_
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import scrape
import triage

MODES = {"transit": "TRANSIT", "drive": "DRIVE", "bicycle": "BICYCLE", "walk": "WALK"}
FIELDS = ",".join(["routes.duration", "routes.distanceMeters", "routes.legs.steps.travelMode",
                   "routes.legs.steps.staticDuration", "routes.legs.steps.transitDetails.stopDetails.arrivalTime",
                   "routes.legs.steps.transitDetails.transitLine.nameShort",
                   "routes.legs.steps.transitDetails.transitLine.name",
                   "routes.legs.steps.transitDetails.transitLine.vehicle.type"])


WORKERS = 16  # Routes API allows 3000 requests/min; triage.post backs off on 429


def progress(done, total, failed, elapsed):
    """'Commute: 120/3124 (4%), 30/s, ~2 min left' for the log and the run pill."""
    rate = done / elapsed if elapsed > 0 else 0
    left = f", ~{max(1, round((total - done) / rate / 60))} min left" if rate and done < total else ""
    return (f"Commute: {done}/{total} ({100 * done // total}%), {rate:.0f}/s{left}"
            + (f", {failed} failed" if failed else ""))


def api_key():
    return os.environ.get("GOOGLE_MAPS_API_KEY") or scrape.load(triage.SECRETS, {}).get("google_maps")


def weekday_at(hhmm, night=False):
    """Next Tuesday at hh:mm (Zurich): a typical weekday, within Google's 100-day timetable window.
    night=True: the night after, for times like 00:30 that belong to Tuesday evening."""
    d = date.today() + timedelta(days=1)
    d += timedelta(days=(1 - d.weekday()) % 7)
    t = time.fromisoformat(hhmm)
    if night and t < time(12):
        d += timedelta(days=1)
    return datetime.combine(d, t, ZoneInfo("Europe/Zurich"))


def place(p):
    return {"location": {"latLng": {"latitude": p[0], "longitude": p[1]}}} if isinstance(p, tuple) else {"address": p}


def parse(rt, depart_at=None):
    """{min, km, rides: [{type, line}], wait_min} for one route; wait_min = door to door from depart_at."""
    steps = [s for leg in rt.get("legs", []) for s in leg.get("steps", [])]
    rides = [{"type": (td := s["transitDetails"]).get("transitLine", {}).get("vehicle", {}).get("type", "OTHER"),
              "line": td.get("transitLine", {}).get("nameShort") or td.get("transitLine", {}).get("name", "?")}
             for s in steps if s.get("transitDetails")]
    r = {"min": round(int(rt["duration"].rstrip("s")) / 60), "km": round(rt.get("distanceMeters", 0) / 1000, 1), "rides": rides}
    if depart_at:
        last = max((i for i, s in enumerate(steps) if s.get("transitDetails")), default=None)
        if last is None:  # walking only
            r["wait_min"] = r["min"]
        else:
            arrive = datetime.fromisoformat(steps[last]["transitDetails"]["stopDetails"]["arrivalTime"].replace("Z", "+00:00"))
            walk = sum(int(s.get("staticDuration", "0s").rstrip("s")) for s in steps[last + 1:])
            r["wait_min"] = round(((arrive - depart_at).total_seconds() + walk) / 60)
    return r


def routes(origin, destination, mode, arrive_by=None, depart_at=None, key=None):
    """All route alternatives (parsed), [] if there is no route."""
    body = {"origin": place(origin), "destination": place(destination), "travelMode": MODES[mode],
            "languageCode": "de-CH", "units": "METRIC"}
    if mode == "transit":
        body["computeAlternativeRoutes"] = True
        if arrive_by:
            body["arrivalTime"] = arrive_by.isoformat()
        if depart_at:
            body["departureTime"] = depart_at.isoformat()
    elif mode == "drive":
        body["routingPreference"] = "TRAFFIC_UNAWARE"  # No rush-hour traffic; TRAFFIC_AWARE costs more.
    res = triage.post("https://routes.googleapis.com/directions/v2:computeRoutes", body,  # retries with backoff
                      {"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS}, timeout=30)
    return [parse(rt, depart_at) for rt in res.get("routes", [])]


def requests_for(l, p):
    """(cache key, request kwargs) for every route this listing/destination pair needs."""
    origin = (l["lat"], l["lon"])
    for addr in scrape.addresses(p):
        yield scrape.commute_key(l, p, addr), dict(origin=origin, destination=addr, mode=p["mode"],
                                                   arrive_by=weekday_at(p["arrive_by"]) if p["mode"] == "transit" else None)
        if p["mode"] == "transit" and p.get("return_at"):
            yield scrape.commute_key(l, p, addr, back=True), dict(
                origin=addr, destination=origin, mode="transit", depart_at=weekday_at(p["return_at"], night=True))


def run(state, log=print):
    points = state["commute"]["points"]
    if not points:
        log("Commute: no destinations configured")
        return
    if not (key := api_key()):
        log("Commute: no Google Maps API key; add it in the UI (or set GOOGLE_MAPS_API_KEY)")
        return
    cache = scrape.load(scrape.COMMUTE, {})
    listings = scrape.annotate(scrape.load_listings(), state)
    todo = list({ck: kw for l in listings.values()
                 if l["lat"] is not None and (not l["excluded"] or l["excluded"].startswith(("AI:", "commute:")))
                 for p in points for ck, kw in requests_for(l, p) if ck not in cache}.items())
    log(f"Commute: {len(todo)} route request(s) for {len(points)} destination(s)")

    def one(item):
        ck, kw = item
        try:
            return ck, routes(**kw, key=key)
        except triage.Fatal:
            raise
        except Exception as e:
            log(f"  {kw['destination'] if isinstance(kw['destination'], str) else kw['origin']}: {e}")
            return ck, None

    # first request alone: a bad key / disabled API fails once with a clear message
    try:
        first = one(todo[0]) if todo else None
    except triage.Fatal as e:
        log(f"  {e}")
        first = None
    if todo and (not first or first[1] is None):
        log("Commute stopped: fix the error above (key, Routes API enabled, billing) and retry")
        return
    done, failed, t0, last = 1, 0, time_.monotonic(), 0.0
    if first:
        cache[first[0]] = first[1]
    with ThreadPoolExecutor(WORKERS) as ex:
        futures = [ex.submit(one, item) for item in todo[1:]]
        try:
            for f in as_completed(futures):  # as they finish, so progress shows while the rest are in flight
                ck, r = f.result()
                done += 1
                if r is None:
                    failed += 1
                else:
                    cache[ck] = r
                if (now := time_.monotonic()) - last >= 2 or done == len(todo):  # every 2 s: the UI polls every 1.5 s
                    last = now
                    scrape.save(scrape.COMMUTE, cache)
                    log(progress(done, len(todo), failed, now - t0))
        except triage.Fatal as e:
            for f in futures:
                f.cancel()
            scrape.save(scrape.COMMUTE, cache)
            log(f"Commute stopped after {done}/{len(todo)}: {e}")
            return
    eligible = sum(not l["excluded"] for l in scrape.write_eligible(scrape.load_state()).values())
    log(f"Commute done: {eligible} eligible")


if __name__ == "__main__":
    run(scrape.load_state())
