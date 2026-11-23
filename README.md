# Swiss Flat Finder

Scrapes rental searches on homegate.ch, immoscout24.ch, newhome.ch and flatfox.ch into one JSON file, with a local web UI for review.

All three sites block plain HTTP (DataDome / Cloudflare), so it drives a **visible Chrome** via Playwright with a persistent profile (`data/browser-profile`). If a captcha appears, solve it in that window; the scraper waits up to 5 min and the cookies are reused next time.

## Setup

```sh
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt   # uses installed Google Chrome
```

## Use

```sh
.venv/bin/python app.py        # UI at http://localhost:8765: set search, scrape, review, block, hide
.venv/bin/python scrape.py     # same scrape without the UI, using saved settings (still opens Chrome)
.venv/bin/python test_scrape.py
```

## Browsing and triage

The **Listings** tab shows listings as cards, a sortable table, or a map (OpenStreetMap via Leaflet, loaded from unpkg), with a detail panel for the selected one: photo gallery (full screen with `f`), key facts, AI flags, commute, description and actions (hide, re-read, block provider). Settings live in the **Settings** tab.

Your own triage is kept per listing in `data/notes.json` and added to `eligible.json` as `user`:

- **Status**: inbox, maybe, shortlist, contacted, viewing, applied (keys `0`–`5`). Rejecting stays *hide* (`h`), which excludes the listing and moves on to the next one.
- **Star** (`s`), free-form **tags** (`t`) and a **note** (`n`, saved as you type).
- **Pinned photos** (`p`): pinned ones come first and the first becomes the card's cover and map preview.
- **Opened**: listings you haven't opened yet carry a *new* badge; filter with *Unopened*, or mark the shown ones as opened.

Every edit, and hiding a listing, shows at once and is saved in the background (notes to `data/notes.json`, hides to `state.json`); `eligible.json` follows two seconds after the last change. If `app.py` isn't answering (stopped, restarting), edits wait in the browser and a banner says how many; they're sent as soon as it's back, also after a reload. Notes are never removed when a listing changes or disappears from a portal.

**Excluded areas**: on the map, draw rectangles or polygons (tools on the left, Leaflet-Geoman from unpkg) to hide the listings inside them, say an industrial zone or a motorway. Corners can be edited and whole areas dragged or removed; the panel on the right renames them, switches them off, and shows how many listings each one hides. They're saved in `data/state.json` and excluded as `area: <name>` before commute and AI triage, so those skip them, and they're left out of `eligible.json`.

Filter by status, star, tag or text (title, address, provider, notes, tags, description). Listings you gave a status or a star stay visible when they become excluded (say outside your filters now), with the reason shown, unless you hid them.

**Why excluded**: with *Show excluded* on, a second dropdown narrows the list to one reason, with how many listings each has: hidden by you, AI triage, commute, map area, blocked agency, search filters (rooms, area, rent, availability), copy on another portal, removed from portal, old search, or site not selected. The kind is read from the reason each listing already carries (`hidden`, `AI: …`, `commute: …`, `area: …`, `blocked: …`, `duplicate of …`, …), so existing data works as is. A listing you hid counts as *hidden by you* whatever else excludes it.

**Removed listings**: a listing taken off its portal is never deleted here. It's flagged `deleted_at` (the time a scrape first found it gone; `last_seen` is the last time it was in the results) and keeps your status, star, tags and notes. The filter next to *Show excluded* hides them (the default), shows them, or shows only them, with how many there are; cards, table and detail panel say *removed from homegate 2026-10-20*. Removals are found by a **Full scrape** or a listing's ↻, not by the quick scrape. A listing that comes back online loses the flag. `?` lists all keyboard shortcuts.

**Download PDF (maybe + shortlist)** exports every listing marked Maybe or Shortlist (hidden ones left out): an overview map and summary table whose numbered pins and rows link to each listing, then one compact block per listing with a key-facts strip, your notes, tags, commute, AI findings, a zoomed-out locator map, all photos (pinned first), the description and a link back to the overview. `app.py` prints `export.html` with headless Chrome via Playwright (`pdf.py`): photos are downloaded 16 at a time while Chrome starts, shrunk to 360 px (JPEG, 65 %) and kept with the map tiles in `data/pdf-cache`, so later exports only download what's new. You can also open http://localhost:8765/export.html and print it from the browser.

Locations: type a town, district, ZIP or canton in the UI (`Meilen (Bezirk)`, `Bezirk Meilen`, `Kanton Zürich`, `8706`; Enter adds the top match); suggestions come from homegate's geo API and are mapped to each site's slug. Multiple locations are scraped one after another. newhome has no ZIP search, so ZIPs are skipped there (add the town instead).

## Publishing (read-only page on Azure)

**Publish** (top bar) or `python publish.py` uploads the results to a private, read-only copy of the Listings tab on [Azure Static Web Apps](https://learn.microsoft.com/azure/static-web-apps/) (Free plan, CHF 0). The scraper stays local; each publish replaces the whole page with your current listings.

- Included: the flats you marked **Maybe** or **Shortlist** (not hidden, also when since removed from the portal), with stars, tags, notes and pinned photos, as cards, table and map. Photos and portal pages are links to the portals.
- Left out: settings, API keys, your income (only whether taxes are on) and commute addresses (only destination names).
- View only: one self-contained page (styles, script and data inline) without scraping, settings, editing or downloads. Change statuses and notes locally and publish again.
- Only people you invite can open it (Microsoft or GitHub login); everyone else gets the sign-in page. `python publish.py --build` only builds `data/site/`.

One-time setup (needs [Node.js](https://nodejs.org) for `npx` and the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), signed in with `az login`):

```sh
.venv/bin/python publish.py --setup --invite github:YOUR_LOGIN   # or aad:you@example.com for a Microsoft account
```

This creates resource group `flats` with a Free Static Web App `flats` in East US 2 (West Europe, the only European region, isn't accepting new apps; the page is served from Azure's global CDN either way) (change with `AZURE_SWA_GROUP`, `AZURE_SWA_NAME`, `AZURE_SWA_LOCATION`), saves its deployment token to `deploy/.env` as `AZURE_STATIC_WEB_APPS_API_TOKEN`, publishes, and prints the site URL and an invitation link. Open the link signed in as that person to give them the `reader` role. Invite others later with the **Invite** button (a GitHub login or a Microsoft account's email; it shows the link to send, with a copy button) or `publish.py --invite github:LOGIN` (links are valid 7 days). Afterwards **Publish** is all you need.

## Tax-adjusted rent

Set your income/age/marital status/children in the UI's Tax box (same inputs as [swisstaxmap.ch](https://www.swisstaxmap.ch)). Each listing's ZIP is sent to swisstaxmap's `calculate-tax` API (cached in `data/taxes.json`), adding `tax_year_chf` and `tax_commune`. The baseline is the lowest-tax commune among the current search's listings; `tax_extra_month_chf` = (tax − baseline) / 12 and `rent_tax_adjusted_chf` = rent + that, i.e. what the flat effectively costs compared to living in the cheapest-tax town.

## AI triage

`triage.py` sends each otherwise-eligible listing's facts, description and up to N photos (800px) to Anthropic or OpenAI (pick in the UI) and gets back structured flags: `temporary` (+ end date), `furnished`, `swap` (Tauschwohnung), `shared` (WG), `kitchen` size, violations of your own free-text rules, evidence quotes and a summary. Tick which findings exclude a listing; results are cached in `data/triage.json` and re-run when provider/model/rules/photo count change. newhome descriptions are fetched from its detail API once per new listing during scraping.

Default models: `claude-sonnet-5-5` and `gpt-6-luna` (about $0.10 / $0.50 per million input / output tokens, roughly 1/40 of `gpt-5.6-sol`). OpenAI requests use flex processing (half price, slower) and fall back to the standard tier when flex has no capacity. Pick another model in the UI and check it against your own verdicts with `python calibrate.py`.

API keys: paste into the UI (stored in `data/secrets.json`, mode 600, never sent back to the page) or set `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, in the shell or in `deploy/.env` (copy `deploy/.env.example`; gitignored, loaded by every script at startup, shell variables win). `python triage.py` runs it from the CLI.

Listings are triaged 8 at a time (`TRIAGE_CONCURRENCY` to change it), with photos downloaded one at a time per listing. A 429/overloaded answer pauses every request to that provider for the `retry-after` time instead of only the one that got it. Lower the concurrency if your API tier keeps hitting rate limits. Each result is saved as soon as it arrives. When credits run out or the key is rejected, the run stops instead of sending the rest of the queue, and the next run (button or `python triage.py`) only triages the listings that are still missing.

## Commute

Add destinations in the UI. Each has a name, mode (transit/drive/bicycle/walk), one or more addresses (the best route to any of them counts, e.g. "Zürich HB" and "Opernhaus, Zürich", so a direct tram to the opera beats a bus + train to HB), arrival time and optional limits: max minutes / km, max transfers (optionally not counting train-to-train changes), and a night-return check (leave the destination at e.g. 00:30, be home within N minutes, waiting included: catches buses that stop running).

`commute.py` asks the Google Maps Routes API (`computeRoutes`) for each otherwise-eligible listing and caches the raw route alternatives (lines and vehicle types) in `data/commute.json`, so changing limits needs no new requests. Transit arrives on a coming Tuesday; driving is without traffic. Missing data (no route found, not computed yet) never excludes. Key: UI field or `GOOGLE_MAPS_API_KEY`; enable the Routes API for its project.

## Flatfox

flatfox.ch has a public JSON API (no browser, no captchas) but searches by map rectangle only. Each location's rectangle comes from swisstopo's geo search (cached in `data/areas.json`); results are then kept only if their ZIP belongs to the location (homegate geo hierarchy, cached in `data/zips.json`). One request lists all matches; only new listings' details are fetched (`data/flatfox.json`). Areas with more than 400 matches are split into quarters. Listings also published on homegate (`smg_id`) are grouped with their homegate copy (see [Duplicates](#duplicates)).

The small **new** button next to each portal scrapes only that portal (new listings), without changing which portals are ticked.

## Quick vs full scrape

- **Save & scrape new listings** (quick): results are sorted newest first; each location stops at the first result page with no new listing ids. Existing listings aren't re-read (listing pages, newhome descriptions, AI and commute results are all reused).
- **Full scrape (+ removed)**: reads every result page. Listings these searches found before but not anymore are checked on their own page: removed ones (404/410 or redirected away) move to *deleted* (kept, with the date), still-online ones (e.g. now outside your filters) are refreshed, unclear ones are re-checked next time. Locations stopped by *Max pages* can't detect removals.

Listings found only by searches you no longer run are shown as *other search*.

## Data quality

Search results can lag behind edits (or show another unit of a new-build project), so the listing's own page is the source of truth: it is read once per new homegate/immoscout24 listing during scraping (availability plus the full listing object). Values contradicted by the listing's own text or by the other portal get a ⚠ and never exclude; ↻ (per listing, or for all conflicting ones) re-reads a listing from its page. AI results are tied to the listing content, so refreshed listings are re-triaged.

`python calibrate.py` checks the AI prompt against listings you've judged yourself (kitchen sizes, criteria); add a case whenever the AI gets one wrong.

## Files (`data/`)

- `state.json`: search params, blocklist, hidden listing keys (edited via UI)
- `listings.json`: every listing seen, keyed `site:id`, with normalized fields + `raw` site JSON
- `eligible.json`: **the output for post-processing**: listings from the latest scrape of each selected site that are not hidden, blocked, duplicated, or outside the search filters
- `taxes.json`, `logos.json`, `triage.json`: tax, logo hash and AI triage caches
- `site/`: the last published page (`publish.py`)
- `pdf-cache/`: photos (shrunk), map tiles and Leaflet for the PDF export; safe to delete
- `commute.json`: route cache
- `photos.json`: photo fingerprints for finding duplicates (`dedup.py`)
- `notes.json`: your status, star, tags, note, pinned photos and opened date per listing
- `secrets.json`: API keys

Normalized fields: `site, id, title, url, rooms, area_m2, rent_chf, address, zip, city, available_from, published, provider, provider_logo, provider_key, provider_label, tax_year_chf, tax_commune, tax_extra_month_chf, rent_tax_adjusted_chf, description, images, ai, user, first_seen, last_seen, raw`.

## Blocklist & provider labels

New setups start with an empty blocklist and no provider labels. To opt in to the [provider preset](providers.example.json), stop the app and merge its `blocklist` and `provider_labels` into `data/state.json`, keeping existing entries and labels on conflicts. If no state file exists, create it from the example. The preset includes logo hashes, not images, and is never loaded automatically.

Each blocklist entry is a case-insensitive substring matched against the provider name/label and the listing's entire raw JSON (description, logo URL…). An entry also matches its name without company form, country or "Real Estate" words, as a whole word: "Apleona Schweiz AG" blocks "Apleona" and "Apleona Real Estate AG" too.

homegate/immoscout24 search results have no provider *name*, only a logo. Logos are downloaded once and identified by content hash (`data/logos.json`); both sites serve identical bytes. In a listing's detail panel, type a name into the logo's label field (Enter saves it; it applies to every listing with that logo), or type it and click "Block provider" to label and block in one go. newhome listings carry real names (`Livit AG`), so a "Livit" entry blocks across all three sites once the SMG logo is labelled.

## Duplicates

Agencies often publish one flat on several portals. Each flat is shown once, as its homegate copy if there is one (then immoscout24, flatfox, newhome); the others are excluded as `duplicate of homegate` etc. and show with *show excluded*. The detail panel links the other copies (*also on*).

- homegate and immoscout24 share inventory and ids, and flatfox knows the homegate id, so those always group.
- Other copies are found in two steps. First, cheaply, pairs on different portals with the same rooms, area within 3 m² or 6 %, rent within CHF 50 or 12 %, and the same street and number or coordinates within 150 m. Then each pair is confirmed by its photos: `dedup.py` downloads the first 5 photos of these listings only and keeps a 64-bit perceptual hash of each (`data/photos.json`), so the same photo resized by another portal still matches. Two matching photos make a duplicate (one if a listing has only one photo); a photo that several listings of one portal share (a building or project rendering) doesn't count. Without photos, or when the portals show different ones, the same address, area within 1 m², rent within 2 % and largely the same description (shared 3-word phrases) do. Two copies with different house numbers need matching photos.
- A flat has at most one listing per portal, so units of one building with the same text stay apart.

Your notes apply to the whole flat: the shown copy carries the furthest status, every star, tag and note of all copies, and an edit goes to all of them (pinned photos stay per portal). Hiding a flat hides all copies; a flat you hid on one portal stays hidden on the others, unless you gave a copy you didn't hide a status or a star. Blocking a provider also blocks the flat on portals that only show its logo.

`dedup.py` runs after each scrape and when `app.py` starts; `python dedup.py` runs it by hand. Photos are downloaded once.
