"""Photo fingerprints for recognising one flat on several portals.

    python dedup.py     # hash the photos of listings that might be duplicates, then rewrite eligible.json

scrape.dup_candidates() narrows the listings to pairs with the same rooms, a close area and rent, and the same
address or coordinates; only those listings' first photos are downloaded and reduced to a 64-bit difference hash
(dHash: is each pixel of a 9x8 greyscale thumbnail brighter than its right neighbour). The same photo resized or
recompressed by another portal keeps nearly the same hash. Hashes are cached by URL in data/photos.json, so each
photo is downloaded once; scrape.find_duplicates() compares them.
"""
import io
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image

import scrape

WORKERS = 16


def dhash(body):
    im = Image.open(io.BytesIO(body))
    im.draft("L", (64, 64))  # JPEG: decode at reduced size, much faster for large originals
    px = list(im.convert("L").resize((9, 8), Image.LANCZOS).getdata())
    bits = "".join("1" if px[r * 9 + c] > px[r * 9 + c + 1] else "0" for r in range(8) for c in range(8))
    return f"{int(bits, 2):016x}"


def fetch(url):
    """The photo's hash; "" if it is gone or not an image (not tried again), None on a network error (tried next run)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
            return dhash(r.read())
    except urllib.error.HTTPError as e:
        return "" if e.code in (403, 404, 410) else None
    except (OSError, ValueError):  # timeouts, resets, DNS
        return None
    except Exception:  # PIL can't read it
        return ""


def todo(state, cache):
    listings = scrape.load_listings()
    locs = {x["id"] for x in state["search"]["locations"]}
    pool = {k: l for k, l in listings.items() if l["site"] in state["search"]["sites"]
            and not l.get("deleted_at") and set(l.get("found_in") or []) & locs}
    keys = {k for pair in scrape.dup_candidates(pool) for k in pair}
    return list(dict.fromkeys(u for k in sorted(keys) for u in pool[k]["images"][:scrape.DUP_PHOTOS] if u not in cache))


def run(state, log=print):
    cache = scrape.load(scrape.PHOTOS, {})
    urls = todo(state, cache)
    log(f"Duplicates: {len(urls)} photo(s) to compare")
    done, failed, last = 0, 0, time.monotonic()
    with ThreadPoolExecutor(WORKERS) as ex:
        futures = {ex.submit(fetch, u): u for u in urls}
        for f in as_completed(futures):
            done += 1
            if (h := f.result()) is None:
                failed += 1
            else:
                cache[futures[f]] = h
            if (now := time.monotonic()) - last >= 2 or done == len(urls):
                last = now
                scrape.save(scrape.PHOTOS, cache)
                log(f"Duplicates: {done}/{len(urls)} photos" + (f", {failed} failed" if failed else ""))
    listings = scrape.write_eligible(scrape.load_state())
    flats = {tuple(f) for f in scrape._clusters.values()}
    log(f"Duplicates done: {len(flats)} flats on more than one portal, "
        f"{sum(len(f) - 1 for f in flats)} copies merged; {sum(not l['excluded'] for l in listings.values())} eligible")


if __name__ == "__main__":
    run(scrape.load_state())
