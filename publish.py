"""Publish the results (not the scraper) as a read-only static site on Azure Static Web Apps.

    python publish.py            # build data/site/ and deploy it (needs AZURE_STATIC_WEB_APPS_API_TOKEN and Node's npx)
    python publish.py --build    # only build data/site/, e.g. to look at it with: python -m http.server -d data/site
    python publish.py --setup [--invite github:LOGIN|aad:EMAIL ...]
                                 # once, needs the Azure CLI signed in (az login): creates the Free Static Web App,
                                 # saves its deployment token to deploy/.env and prints invitation links
    python publish.py --invite github:LOGIN|aad:EMAIL ...   # invite more people later

The site is the Listings tab of the local UI, view only, as one self-contained index.html: the flats you marked Maybe or
Shortlist (not hidden), with your statuses, stars, tags, notes and pinned photos.
Photos and portal pages stay links to the portals. Settings, API keys, your income and commute addresses are left out.
staticwebapp.config.json lets in only people you invited with the "reader" role (see README: Publishing).
"""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import scrape

ROOT = Path(__file__).parent

SITE = scrape.DATA / "site"
TOKEN_VAR = "AZURE_STATIC_WEB_APPS_API_TOKEN"
# where --setup puts it; override with AZURE_SWA_NAME / AZURE_SWA_GROUP / AZURE_SWA_LOCATION
AZ_NAME = os.environ.get("AZURE_SWA_NAME", "flats")
AZ_GROUP = os.environ.get("AZURE_SWA_GROUP", "flats")
# westeurope, the only European region for Static Web Apps, refuses new customers; the page is served from Azure's
# global CDN anyway, so the region only decides where the app resource lives
AZ_LOCATION = os.environ.get("AZURE_SWA_LOCATION", "eastus2")
CONFIG = {
    # everything, data.json included, only for invited users; login.html lets the others sign in
    "routes": [{"route": "/login.html", "allowedRoles": ["anonymous", "authenticated"]},
               {"route": "/*", "allowedRoles": ["reader"]}],
    "responseOverrides": {"401": {"redirect": "/login.html", "statusCode": 302},
                          "403": {"rewrite": "/login.html"}},
    "globalHeaders": {"X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer",
                      "Cache-Control": "no-cache"},
}
LOGIN = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flats: sign in</title>
<style>
  body { font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 26em; margin: 15vh auto; padding: 0 16px; }
  a { display: block; margin: .6em 0; padding: .6em 1em; border: 1px solid #ccc; border-radius: 6px; text-decoration: none; color: inherit; }
  @media (prefers-color-scheme: dark) { body { background: #111318; color: #e6e8ec; } a { border-color: #2e333d; } }
</style>
<h1>Flats</h1>
<p>Private page. Sign in with the account you were invited with.</p>
<a href="/.auth/login/aad?post_login_redirect_uri=/">Sign in with Microsoft</a>
<a href="/.auth/login/github?post_login_redirect_uri=/">Sign in with GitHub</a>
<p><small>Signed in but sent back here? That account hasn't been invited. <a href="/.auth/logout?post_logout_redirect_uri=/login.html">Sign out</a></small></p>
</html>
"""


PUBLISHED_STATUSES = ("maybe", "shortlist")


def published(listings):
    """The flats you marked Maybe or Shortlist and didn't hide (also when since removed from the portal)."""
    return {k: l for k, l in listings.items()
            if (l.get("user") or {}).get("status") in PUBLISHED_STATUSES and l.get("excluded") != "hidden"}


# Parts of index.html the view-only page leaves out: scraping, settings, editing, downloads and the area editor.
# Elements app.js still looks up are hidden instead (and by the .static rules in app.css).
DROP = [
    r'\s*<link rel="stylesheet" href="https://unpkg.com/@geoman-io[^>]*>',
    r'\s*<script src="https://unpkg.com/@geoman-io[^>]*></script>',
    r'\s*<nav class="tabs".*?</nav>',
    r'\s*<a href="/api/export".*?</a>',
    r'\s*<dialog id="invite">.*?</dialog>',
    r'(?<=<main id="tab-settings" hidden>).*?(?=</main>)',
    r'\s*<tr><td><kbd>(?:p|0|s|h|n|t)</kbd>.*?</tr>',
]
HIDE = [r'<button id="(?:scrape|scrapeFull|publishBtn|inviteBtn|logToggle|markSeen|pdfBtn|lbPin)"',
        r'<label class="toggle"(?=><input type="checkbox" id="(?:f-unseen|showExcluded)")']


def view_only(index, css, js, data):
    """index.html as one self-contained view-only page: styles, script and data inline, so nothing else has to load."""
    for pattern in DROP:
        index, n = re.subn(pattern, "", index, flags=re.S)
        if not n:
            raise RuntimeError(f"index.html changed: nothing matches {pattern!r} (update publish.DROP)")
    for pattern in HIDE:
        index, n = re.subn(pattern, lambda m: m.group(0) + " hidden", index)
        if not n:
            raise RuntimeError(f"index.html changed: nothing matches {pattern!r} (update publish.HIDE)")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")  # can't end the <script> early
    swaps = {'<html lang="en">': '<html lang="en" class="static">',
             "<title>Flat scraper</title>": "<title>Flats: maybe + shortlist</title>",
             '<link rel="stylesheet" href="/static/app.css">': f"<style>\n{css}</style>",
             '<script src="/static/app.js"></script>':
                 f"<script>window.STATIC_SITE = true;\nwindow.STATIC_DATA = {payload};</script>\n<script>\n{js}</script>"}
    for old, new in swaps.items():
        if old not in index:
            raise RuntimeError(f"index.html changed: {old!r} not found")
        index = index.replace(old, new, 1)
    return index


def public_state(state):
    """Only what the listings view reads: whether taxes are on, and commute destination names (no addresses)."""
    return {"tax": {"income": 1 if state["tax"]["income"] > 0 else 0},
            "commute": {"points": [{"name": p["name"], "mode": p["mode"], "return_at": p.get("return_at", "")}
                                   for p in state["commute"]["points"]]}}


def build(log=print):
    listings = published(scrape.annotate(scrape.load_listings(), scrape.load_state()))
    for l in listings.values():
        l.pop("raw", None)
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir(parents=True)
    data = {"state": public_state(scrape.load_state()), "version": "static", "run": {"running": False, "log": []},
            "published_at": datetime.now().isoformat(timespec="minutes"), "listings": listings}
    read = lambda f: (ROOT / f).read_text(encoding="utf-8")
    page = view_only(read("index.html"), read("static/app.css"), read("static/app.js"), data)
    (SITE / "index.html").write_text(page, encoding="utf-8")
    (SITE / "login.html").write_text(LOGIN, encoding="utf-8")
    (SITE / "staticwebapp.config.json").write_text(json.dumps(CONFIG, indent=2))
    log(f"built {SITE / 'index.html'} with {len(listings)} listings (maybe + shortlist)")


def deploy(log=print):
    if not os.environ.get(TOKEN_VAR):
        raise RuntimeError(f"{TOKEN_VAR} is not set: put the Static Web App's deployment token in deploy/.env")
    if not shutil.which("npx"):
        raise RuntimeError("npx not found: install Node.js (e.g. brew install node) to deploy")
    # the token goes in by environment variable, so it doesn't show up in the process list
    cmd = ["npx", "--yes", "@azure/static-web-apps-cli", "deploy", str(SITE), "--env", "production"]
    env = os.environ | {"SWA_CLI_DEPLOYMENT_TOKEN": os.environ[TOKEN_VAR]}
    log("deploying to Azure Static Web Apps…")
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    for line in (p.stdout + p.stderr).splitlines():
        if line.strip():
            log(line)
    if p.returncode:
        raise RuntimeError(f"deploy failed (exit {p.returncode})")
    log("published")


def az(*args):
    """Run an Azure CLI command and return its output (stripped)."""
    if not shutil.which("az"):
        raise RuntimeError("Azure CLI not found: install it (brew install azure-cli) and run az login")
    p = subprocess.run(["az", *args, "--only-show-errors"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if p.returncode:
        raise RuntimeError(f"az {' '.join(args[:2])} failed: {p.stderr.strip()}")
    return p.stdout.strip()


def set_env(key, value, path=scrape.ENV_FILE):
    """Set key=value in deploy/.env, replacing an existing (or commented-out) line for the key."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    keep = [l for l in lines if l.strip().removeprefix("#").strip().split("=", 1)[0].strip() != key]
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(keep + [f"{key}={value}"]) + "\n", encoding="utf-8")
    path.chmod(0o600)
    os.environ[key] = value


def hostname():
    return az("staticwebapp", "show", "-n", AZ_NAME, "-g", AZ_GROUP, "--query", "defaultHostname", "-o", "tsv")


def setup(log=print):
    """Create the Free Static Web App (if missing) with the Azure CLI and save its deployment token."""
    log("Azure account: " + az("account", "show", "--query", "[name, user.name]", "-o", "tsv").replace("\n", ", "))
    if az("group", "exists", "-n", AZ_GROUP) != "true":  # an existing group may be in another region: that's fine
        az("group", "create", "-n", AZ_GROUP, "-l", AZ_LOCATION)
    if az("staticwebapp", "list", "-g", AZ_GROUP, "--query", f"[?name=='{AZ_NAME}'].name", "-o", "tsv"):
        log(f"static web app {AZ_NAME} exists")
    else:  # no --source: deployed from here by publish.py, not from a repo
        az("staticwebapp", "create", "-n", AZ_NAME, "-g", AZ_GROUP, "-l", AZ_LOCATION, "--sku", "Free")
        log(f"created static web app {AZ_NAME} in resource group {AZ_GROUP} ({AZ_LOCATION}, Free plan)")
    token = az("staticwebapp", "secrets", "list", "-n", AZ_NAME, "-g", AZ_GROUP, "--query", "properties.apiKey", "-o", "tsv")
    set_env(TOKEN_VAR, token)
    log(f"saved the deployment token to {scrape.ENV_FILE}; site: https://{hostname()}")


def invite(who, log=print):
    """who: github:LOGIN or aad:EMAIL, or just a GitHub login or a Microsoft account's email.
    Logs and returns the link that person opens (signed in with that account) to get the reader role."""
    who = who.strip()
    provider, sep, user = who.partition(":")
    if not sep:  # no prefix: an email is a Microsoft account, anything else a GitHub login
        provider, user = ("aad" if "@" in who else "github"), who
    provider = {"github": "GitHub", "aad": "AAD", "microsoft": "AAD"}.get(provider.lower())
    user = user.strip().removeprefix("@")
    if not provider or not user or any(c.isspace() for c in user):
        raise ValueError(f"{who!r}: use a GitHub login, an email, github:LOGIN or aad:EMAIL")
    url = az("staticwebapp", "users", "invite", "-n", AZ_NAME, "-g", AZ_GROUP, "--authentication-provider", provider,
             "--user-details", user, "--roles", "reader", "--domain", hostname(),
             "--invitation-expiration-in-hours", "168", "--query", "invitationUrl", "-o", "tsv")
    log(f"invitation for {user} (valid 7 days, open it signed in as them): {url}")
    expires = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    return {"url": url, "user": user, "provider": provider, "expires": expires}


def run(log=print, build_only=False):
    build(log)
    if not build_only:
        deploy(log)


if __name__ == "__main__":
    args = sys.argv[1:]
    invites = [a for i, a in enumerate(args) if i and args[i - 1] == "--invite"]
    if "--setup" in args:
        setup()
    for who in invites:
        invite(who)
    if "--setup" in args or not invites:
        run(build_only="--build" in args)
