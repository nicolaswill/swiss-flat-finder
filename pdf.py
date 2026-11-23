"""PDF export: export.html printed by headless Chrome (GET /api/pdf in app.py).

Photos, map tiles and Leaflet are kept in data/pdf-cache, so a second export only downloads what's new.
Photos missing from it are downloaded 16 at a time while Chrome starts (a browser fetches 6 per host),
then served to the page from the cache; export.js shrinks the large ones before printing."""
import base64
import hashlib
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from playwright.sync_api import sync_playwright

import scrape

CACHE = scrape.DATA / "pdf-cache"
STATUSES = ("maybe", "shortlist")  # the statuses export.js shows
TILE_DAYS = 30  # map tiles are re-downloaded after this; photos and Leaflet never change under the same URL
CACHED_HOSTS = ("tile.openstreetmap.org", "unpkg.com")


def cache_path(url):
    return CACHE / hashlib.sha1(url.encode()).hexdigest()


def cached(url):
    """The cached file's content type, or None if it isn't cached (or is an outdated map tile)."""
    p = cache_path(url)
    try:
        if "tile.openstreetmap.org" in url and time.time() - p.stat().st_mtime > TILE_DAYS * 86400:
            return None
        return p.exists() and p.with_suffix(".type").read_text()
    except FileNotFoundError:
        return None


def store(url, body, mime):
    CACHE.mkdir(parents=True, exist_ok=True)
    p = cache_path(url)
    p.with_suffix(".type").write_text(mime)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(body)
    tmp.replace(p)  # the body last: a body without its type would be served as a broken file


def download(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            if (mime := r.headers.get_content_type()).startswith("image/"):
                store(url, r.read(), mime)
    except Exception:
        pass  # the page tries it once more; a photo that still fails is left out


def photos(listings):
    """Photo URLs of the exported listings that aren't cached yet."""
    urls = set()
    for l in listings.values():
        u, imgs = l.get("user") or {}, l.get("images") or []
        if u.get("status") in STATUSES and l.get("excluded") != "hidden" and not l.get("dup_of"):
            urls.update(imgs)
    return [u for u in urls if not cache_path(u).exists()]


def render(url, listings):
    """export.html at url as an A4 PDF, once its maps and photos have loaded."""
    with ThreadPoolExecutor(16) as pool, sync_playwright() as p:
        prefetch = [pool.submit(download, u) for u in photos(listings)]
        try:
            browser = p.chromium.launch(channel="chrome")
        except Exception:  # no Google Chrome installed: Playwright's own Chromium, if downloaded
            browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.route("**/*", serve)
            page.on("response", keep)
            for f in prefetch:
                f.result()
            page.goto(url)
            page.wait_for_function("window.exportReady", timeout=120_000)
            for src, data in page.evaluate("window.shrunk").items():  # next time, no full-size photo to send or shrink
                store(src, base64.b64decode(data.split(",", 1)[1]), "image/jpeg")
            return page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        finally:
            browser.close()


def serve(route):
    """Cached photos, tiles and Leaflet from disk; photos with a CORS header, so export.js may shrink them."""
    r, url = route.request, route.request.url
    if r.method != "GET" or url.startswith("http://127.0.0.1"):
        return route.continue_()
    if hit := cached(url):
        return route.fulfill(path=cache_path(url), content_type=hit, headers={"Access-Control-Allow-Origin": "*"})
    if r.resource_type == "image" and not any(h in url for h in CACHED_HOSTS):  # a photo the prefetch missed
        try:
            res = route.fetch(headers={"User-Agent": "Mozilla/5.0"})
            body, mime = res.body(), res.headers.get("content-type", "")
        except Exception:
            return route.abort()
        if res.ok and mime.startswith("image/"):
            store(url, body, mime.split(";")[0])
        return route.fulfill(status=res.status, body=body, content_type=mime, headers={"Access-Control-Allow-Origin": "*"})
    route.continue_()  # tiles and Leaflet download in parallel as usual; keep() caches them


def keep(response):
    url = response.url
    if response.ok and response.request.method == "GET" and any(h in url for h in CACHED_HOSTS) and not cache_path(url).exists():
        try:
            store(url, response.body(), response.headers.get("content-type", "application/octet-stream").split(";")[0])
        except Exception:
            pass  # e.g. the page closed first

