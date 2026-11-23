'use strict';
// Review UI: settings form (saved as one state object), listings as cards / table / map with a detail panel,
// and your own triage per listing (status, star, tags, note, pinned photos) saved to data/notes.json.

const $ = id => document.getElementById(id);
// Portals send descriptions as plain text or as HTML: keep the formatting tags, drop everything else (scripts, links, styles).
// Same as in export.js.
const KEEP_TAGS = new Set(['B', 'STRONG', 'I', 'EM', 'U', 'BR', 'P', 'UL', 'OL', 'LI']);
const isHtml = s => /<[a-z][^>]*>/i.test(s);
function descHtml(s) {
  if (!isHtml(s)) return esc(s);
  const clean = n => [...n.childNodes].map(c => c.nodeType === 3 ? esc(c.textContent)
    : c.nodeType !== 1 || ['SCRIPT', 'STYLE'].includes(c.tagName) ? ''
    : KEEP_TAGS.has(c.tagName) ? (c.tagName === 'BR' ? '<br>' : `<${c.tagName}>${clean(c)}</${c.tagName}>`)
    : /^(DIV|H\d)$/.test(c.tagName) ? `<p>${clean(c)}</p>` : clean(c)).join('');
  return clean(new DOMParser().parseFromString(s, 'text/html').body);
}
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
const fmt = n => n == null || n === '' ? '–' : typeof n === 'number' ? n.toLocaleString('de-CH') : esc(n);
const ls = (k, def) => { try { return JSON.parse(localStorage[k]) ?? def; } catch { return def; } };
const lsSet = (k, v) => { try { localStorage[k] = JSON.stringify(v); } catch { /* private window */ } };
const post = (url, body = {}) => fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});

// "" = inbox (not triaged yet); rejecting a listing = hiding it (state.hidden), so it isn't a status
const ST = [['', 'Inbox'], ['maybe', 'Maybe'], ['shortlist', 'Shortlist'], ['contacted', 'Contacted'], ['viewing', 'Viewing'], ['applied', 'Applied']];
const stClass = s => 'st-' + (s || 'inbox');
const stLabel = s => (ST.find(([k]) => k === (s || '')) || ST[0])[1];
const GONE = ['deleted', 'other search', 'site not selected'];
// The published copy (publish.py): read-only, listings from data.json, no settings, no saving.
const STATIC = !!window.STATIC_SITE;

let data, selected = ls('selected', null), imgIdx = 0, tab = 'listings', view = ls('view', 'cards');
let [sortKey, sortDir] = ls('sort', ['rent_tax_adjusted_chf', 1]);
// removed: listings taken off their portal (deleted_at): 'hide', 'show' or 'only'; the published copy shows them
// reason: with "show excluded", only the excluded listings of this kind (exclusionKind()); '' = everything
let filters = {q: '', status: 'all', tag: '', unseen: false, excluded: false, reason: '', removed: STATIC ? 'show' : 'hide', ...ls('filters', {}), q: ''};
const user = l => l.user || {};
const cur = () => data && data.listings[selected];

// ---------- load / save ----------

// load(): fill the form from the server, only on page load (or when the user asks after a conflict).
// refresh(): new listings/results/status after saves and runs; never touches the form, so unsaved edits survive.
async function load() {
  data = STATIC ? window.STATIC_DATA : await (await fetch('/api/data')).json();
  applyPending(data.listings); flushSaves();
  $('conflict').hidden = true;
  if (STATIC) {
    $('runpill').textContent = 'published ' + data.published_at.replace('T', ' ');
    return renderList(), renderDetail();
  }
  fillForm(); refreshViews();
}
async function refresh() {
  const d = await (await fetch('/api/data')).json();
  const changed = d.version !== data.version;
  if (changed) applyPending(d.listings);  // edits still on their way to the server stay on the page
  Object.assign(data, {keys: d.keys, run: d.run}, changed ? {listings: d.listings, version: d.version} : {});
  if (changed) refreshViews(); else { showKeyStatus(); showRun(data.run); }
}
function refreshViews() {
  renderList(); renderProviders(); showKeyStatus(); showRun(data.run);
  // don't rebuild the panel under the cursor of someone typing a note or tag
  if (!$('detail').contains(document.activeElement) || !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) renderDetail();
}

function fillForm() {
  const s = data.state.search;
  document.querySelectorAll('[name=site]').forEach(c => c.checked = s.sites.includes(c.value));
  renderLocations(); renderProviders(); renderPoints();
  const tr = data.state.triage;
  $('ai-provider').value = tr.provider; $('ai-max_images').value = tr.max_images;
  $('ai-custom_rules').value = tr.custom_rules; $('ai-auto').checked = tr.auto;
  for (const k of ['furnished', 'swap', 'shared', 'temporary', 'custom']) $('ai-ex-' + k).checked = tr.exclude[k];
  $('ai-ex-temporary_min_until').value = tr.exclude.temporary_min_until; $('ai-ex-kitchen').value = tr.exclude.kitchen;
  showKeyStatus(true);
  for (const k of ['min_rooms', 'min_area', 'max_rent', 'max_pages']) $(k).value = s[k];
  for (const k of ['move_in_earliest', 'move_in_latest']) $(k).value = s[k];
  $('blocklist').value = data.state.blocklist.join('\n');
  const t = data.state.tax;
  for (const k of ['income', 'age', 'income2', 'age2']) $('tax-' + k).value = t[k];
  $('tax-married').checked = t.married;
  $('tax-children').value = t.children.join(', ');
}

function readState() {
  const st = data.state, tr = st.triage;
  st.commute.points = [...document.querySelectorAll('#points .point')].map(row => {
    const v = c => row.querySelector('.p-' + c).value;
    return {name: v('name').trim() || 'Destination', addresses: v('addresses').trim(), mode: v('mode'), arrive_by: v('arrive_by') || '08:30',
      max_minutes: +v('max_minutes') || 0, max_km: +v('max_km') || 0, max_transfers: v('max_transfers') === '' ? null : +v('max_transfers'),
      free_train_transfers: row.querySelector('.p-free_train_transfers').checked, return_at: v('return_at'),
      max_night_minutes: +v('max_night_minutes') || 0};
  }).filter(p => p.addresses);
  st.commute.auto = $('commute-auto').checked;
  for (const p of ['anthropic', 'openai']) tr.models[p] = $('model-' + p).value.trim();
  Object.assign(tr, {provider: $('ai-provider').value, max_images: +$('ai-max_images').value,
    custom_rules: $('ai-custom_rules').value, auto: $('ai-auto').checked});
  for (const k of ['furnished', 'swap', 'shared', 'temporary', 'custom']) tr.exclude[k] = $('ai-ex-' + k).checked;
  tr.exclude.temporary_min_until = $('ai-ex-temporary_min_until').value; tr.exclude.kitchen = $('ai-ex-kitchen').value;
  st.search.sites = [...document.querySelectorAll('[name=site]:checked')].map(c => c.value);
  for (const k of ['min_rooms', 'min_area', 'max_rent', 'max_pages']) st.search[k] = +$(k).value;
  for (const k of ['move_in_earliest', 'move_in_latest']) st.search[k] = $(k).value;
  for (const k of ['income', 'age', 'income2', 'age2']) st.tax[k] = +$('tax-' + k).value;
  st.tax.married = $('tax-married').checked;
  st.tax.children = $('tax-children').value.split(',').map(s => parseInt(s)).filter(n => n >= 0);
  st.blocklist = $('blocklist').value.split('\n').map(s => s.trim()).filter(Boolean);
  return st;
}

// Saves post the whole state, so run them in order: a label's save and a block click must not race.
// A failed save (app.py down or restarting) shows a banner and doesn't stop later saves; the page keeps the
// changes, so the next save or "Retry" sends them.
let saving = Promise.resolve();
function saveState(st = readState()) {
  if (STATIC) return Promise.resolve();
  return saving = saving.then(async () => {
    const r = await post('/api/state', st).catch(() => null);
    $('saveFailed').hidden = !!r?.ok || r?.status === 409;
    if (r?.status === 409) { $('conflict').hidden = false; return; }
    if (!r?.ok) return;
    data.state.rev = (await r.json()).rev;
    await refresh();
  }).catch(e => console.error('save', e));
}

// a flat on several portals is hidden and unhidden with all its copies
const hide = key => queueSave('/api/hidden', {keys: [key, ...(data.listings[key]?.dups || [])], hidden: true});
const unhide = key => queueSave('/api/hidden', {keys: [key, ...(data.listings[key]?.dups || [])], hidden: false});
function setLabel(key, label) {
  const st = readState();
  if (label.trim()) st.provider_labels[key] = label.trim(); else delete st.provider_labels[key];
  saveState(st);
}
// The blocklist is read back from its textarea on every save (readState), so block buttons edit the textarea:
// otherwise the next save, say hiding a listing, would quietly undo the block.
function setBlocklist(list) { $('blocklist').value = list.join('\n'); return saveState(); }
function blockProvider(key) {
  const l = cur(), input = $('provLabel');
  const label = (input ? input.value : l.provider || l.provider_label || '').trim();
  if (!label) { input.placeholder = 'name it first'; input.focus(); return; }
  const st = readState();
  if (key) st.provider_labels[key] = label;
  setBlocklist(st.blocklist.some(b => b.toLowerCase() === label.toLowerCase()) ? st.blocklist : [...st.blocklist, label]);
}
function unblock(term) {
  setBlocklist(readState().blocklist.filter(b => b.toLowerCase() !== term.toLowerCase()));
}
function blockLabel(label) { setBlocklist([...readState().blocklist, label]); }

// ---------- your triage (data/notes.json) ----------

// Triage edits and hides update the page at once. They wait in localStorage until app.py has written them, so a
// server that is down or restarting loses nothing: they're re-sent in order (a later edit never lands before an
// earlier one) until saved.
let pendingSaves = ls('pendingSaves', []), flushing = null;
function applySave({url, body}, listings) {
  for (const k of body.keys) {
    const l = listings[k];
    if (!l) continue;
    if (url === '/api/notes') l.user = {...user(l), ...body.patch};
    else if (body.hidden) l.excluded = 'hidden';
    else if (l.excluded === 'hidden') l.excluded = null;  // its other exclusions, if any, come with the next refresh
  }
}
const applyPending = listings => pendingSaves.forEach(s => applySave(s, listings));
function flushSaves() {
  if (!pendingSaves.length || STATIC) { showUnsaved(); return Promise.resolve(true); }
  return flushing ??= (async () => {
    let hides = false;
    while (pendingSaves.length) {
      const r = await post(pendingSaves[0].url, pendingSaves[0].body).catch(() => null);
      if (!r?.ok) break;
      hides ||= pendingSaves[0].url === '/api/hidden';
      pendingSaves.shift(); lsSet('pendingSaves', pendingSaves);
    }
    flushing = null; showUnsaved();
    if (hides && !pendingSaves.length) refresh().catch(() => {});  // unhidden listings get their exclusions back
    return !pendingSaves.length;
  })();
}
function showUnsaved() {
  const n = pendingSaves.length;
  $('unsaved').hidden = !n;
  $('unsaved').textContent = `⚠ ${n} edit(s) not saved yet: app.py isn't answering. They're kept in this browser and saved as soon as it's back.`;
}
setInterval(() => pendingSaves.length && flushSaves(), 5000);
function queueSave(url, body, rerender = true) {
  body.keys = body.keys.filter(k => data.listings[k]);
  if (!body.keys.length || STATIC) return Promise.resolve(false);
  applySave({url, body}, data.listings);
  if (rerender) { renderList(); renderDetail(); }
  pendingSaves.push({url, body}); lsSet('pendingSaves', pendingSaves);
  return flushing ? flushing.then(flushSaves) : flushSaves();
}
const setUser = (keys, patch, rerender = true) => queueSave('/api/notes', {keys: [].concat(keys), patch}, rerender);
const setStatus = (k, status) => setUser(k, {status});
const toggleStar = k => k && data.listings[k] && setUser(k, {starred: !user(data.listings[k]).starred});
function addTag(value) {
  const t = value.trim(), l = cur();
  if (!t || !l) return;
  setUser(selected, {tags: [...new Set([...(user(l).tags || []), t])]});
  $('tagInput').focus();
}
function removeTag(t) { const l = cur(); setUser(selected, {tags: (user(l).tags || []).filter(x => x !== t)}); }
function allTags() {
  return [...new Set(Object.values(data.listings).flatMap(l => user(l).tags || []))].sort((a, b) => a.localeCompare(b, 'de'));
}
function markAllSeen() {
  const now = new Date().toISOString().slice(0, 19), keys = rows().filter(([, l]) => !user(l).seen).map(([k]) => k);
  setUser(keys, {seen: now});
}

let noteTimer = null, noteKey = null;
function noteInput() {
  noteKey = selected; $('noteState').textContent = 'editing…';
  clearTimeout(noteTimer); noteTimer = setTimeout(flushNote, 700);
}
function flushNote() {
  if (!noteTimer) return;
  clearTimeout(noteTimer); noteTimer = null;
  const k = noteKey, box = $('noteBox');
  if (!box || box.dataset.key !== k) return;
  setUser(k, {note: box.value}, false).then(ok => {
    if (k === selected && $('noteState')) $('noteState').textContent = ok ? 'saved' : 'not saved yet, kept in this browser';
  });
  renderList();
}

// ---------- filtering and sorting ----------

const SORTS = {rent_tax_adjusted_chf: 'Tax-adj. rent', rent_chf: 'Rent', chf_m2: 'CHF per m²', rooms: 'Rooms', area_m2: 'Area', floor: 'Floor',
  first_seen: 'First seen', published: 'Published', available_from: 'Available', commute: 'Commute', status: 'Status', updated: 'Last edited by you'};
const chfM2 = l => l.rent_chf && l.area_m2 ? Math.round(l.rent_chf / l.area_m2 * 10) / 10 : null;
function sortVal(l) {
  switch (sortKey) {
    case 'available_from': return {immediately: '0', 'by arrangement': 'z'}[l.available_from] || l.available_from;
    case 'published': return l.published || l.first_seen;
    case 'address': return `${l.zip} ${l.address}`;
    case 'kitchen': return l.ai && ['tiny', 'small', 'normal', 'large', 'unknown'].indexOf(l.ai.kitchen);
    case 'flags': return l.ai && aiFlags(l.ai);
    case 'commute': return l.commute && l.commute[0] && l.commute[0].min;
    case 'status': return ST.findIndex(([s]) => s === (user(l).status || ''));
    case 'updated': return user(l).updated;
    case 'chf_m2': return chfM2(l);
    case 'floor': return l.floor_n;
    default: return l[sortKey];
  }
}
function compare([, a], [, b]) {
  const x = sortVal(a), y = sortVal(b);
  if (x == null || x === '') return y == null || y === '' ? 0 : 1;  // empty values last, either direction
  if (y == null || y === '') return -1;
  return (typeof x === 'number' ? x - y : String(x).localeCompare(String(y), 'de')) * sortDir;
}
function setSort(k, dir) {
  sortDir = dir ?? (sortKey === k ? -sortDir : 1); sortKey = k;
  lsSet('sort', [sortKey, sortDir]); renderList(true);
}

// Why a listing is excluded, from the reason the server gives it (scrape.annotate_base / overlay), so nothing new is stored.
// Hiding wins over any other reason: a listing you hid shows as "hidden" whatever else excludes it.
const EXCLUSION_KINDS = [['hidden', 'Hidden by you'], ['ai', 'AI triage'], ['commute', 'Commute'], ['area', 'Map area'],
  ['blocked', 'Blocked agency'], ['search', 'Search filters'], ['duplicate', 'Copy on another portal'],
  ['removed', 'Removed from portal'], ['other_search', 'Old search'], ['site', 'Site not selected']];
const exclusionKind = x => !x ? '' : x === 'hidden' ? 'hidden' : x.startsWith('AI: ') ? 'ai' : x.startsWith('commute: ') ? 'commute'
  : x.startsWith('area: ') ? 'area' : x.startsWith('blocked: ') ? 'blocked' : x.startsWith('duplicate of ') ? 'duplicate'
  : x === 'deleted' ? 'removed' : x === 'other search' ? 'other_search' : x === 'site not selected' ? 'site' : 'search';

// Excluded listings are shown with "show excluded", and also when you gave them a status or a star, unless you hid them yourself.
// Listings removed from their portal are kept (with your notes) and follow the removed filter: hidden by default,
// shown (except the ones you hid) or only them.
// Other portals' copies of a flat only show with "show excluded": your notes on them are on the shown copy.
// "Show excluded" with a reason picked shows only the listings excluded for that reason (picking "removed" shows
// removed listings even when the removed filter hides them).
function matches([, l], ignoreStatus = false) {
  const u = user(l), removed = !!l.deleted_at, reason = filters.excluded && filters.reason;
  if (reason && exclusionKind(l.excluded) !== reason) return false;
  if (filters.removed === 'hide' ? removed && reason !== 'removed' : filters.removed === 'only' && !removed) return false;
  if (l.dup_of && !filters.excluded) return false;
  if (l.excluded && !filters.excluded && (l.excluded === 'hidden' || !(removed || u.status || u.starred))) return false;
  if (!ignoreStatus && filters.status !== 'all'
      && (filters.status === 'starred' ? !u.starred : (u.status || 'inbox') !== filters.status)) return false;
  if (filters.tag && !(u.tags || []).includes(filters.tag)) return false;
  if (filters.unseen && u.seen) return false;
  if (filters.q) {
    const hay = [l.title, l.address, l.zip, l.city, l.provider, l.provider_label, l.site, u.note, ...(u.tags || []),
      l.ai && l.ai.summary, l.description].join(' ').toLowerCase();
    if (!filters.q.toLowerCase().split(/\s+/).every(w => hay.includes(w))) return false;
  }
  return true;
}
const rows = () => Object.entries(data.listings).filter(r => matches(r)).sort(compare);
function setFilter(k, v) {
  filters[k] = v;
  lsSet('filters', {...filters, q: ''});
  renderList(true);
}

// ---------- rendering: shared bits ----------

// deleted_at: when a scrape first found the listing gone from its portal (it's kept, with your notes)
const removedText = l => `<span title="Detected ${esc(l.deleted_at.replace('T', ' ').slice(0, 16))}; last seen online ${esc((l.last_seen || '').slice(0, 10))}">removed from ${esc(l.site)} ${esc(l.deleted_at.slice(0, 10))}</span>${l.excluded === 'hidden' ? ' · hidden' : ''}`;
const badge = (text, title, cls) => `<span class="badge ${cls}" title="${esc(title)}">${esc(text)}</span>`;
const FLAG_LABELS = {temporary: 'temporary', furnished: 'furnished', swap: 'Tauschwohnung', shared: 'WG'};
function aiBadges(a, compact = false) {  // red = flagged with evidence; dashed = only one model (consensus mode)
  if (!a) return compact ? '' : '<span class="muted">not triaged</span>';
  const disputed = a.disputed || [], out = [];
  for (const [k, label] of Object.entries(FLAG_LABELS)) {
    const ev = a.evidence[k] || '';
    if (a[k] && ev) out.push(badge(label + (k === 'temporary' && a.temporary_until ? ' → ' + a.temporary_until : ''), ev, 'flag'));
    else if (disputed.includes(k)) out.push(badge(label + '?', 'only one model: ' + ev, 'disputed'));
  }
  for (const x of a.exclusions || [])
    if (x.matches && x.evidence) out.push(badge(x.criterion, x.evidence, 'flag'));
    else if (disputed.includes(x.criterion)) out.push(badge(x.criterion + '?', 'only one model: ' + x.evidence, 'disputed'));
  return out.join(' ') || (compact ? '' : '<span class="muted">–</span>');
}
function aiFlags(a) {
  const f = ['furnished', 'swap', 'shared'].filter(k => a[k]);
  if (a.temporary) f.push('temporary' + (a.temporary_until ? ' → ' + a.temporary_until : ''));
  const rules = (a.exclusions || []).filter(r => r.matches && r.evidence).map(r => r.criterion);
  return [`kitchen: ${a.kitchen}`, ...f, ...rules].join(', ');
}
function kitchenCell(a) {
  if (!a) return '<td></td>';
  const by = a.kitchen_by && new Set(Object.values(a.kitchen_by)).size > 1
    ? `<div class="muted">${Object.entries(a.kitchen_by).map(([m, k]) => `${esc(m)}: ${esc(k)}`).join(', ')}</div>` : '';
  return `<td title="${esc(a.evidence.kitchen || '')}"><span class="k-${esc(a.kitchen)}">${esc(a.kitchen)}</span>${by}</td>`;
}
function commuteCell(c) {
  const pts = data.state.commute.points;
  return (c || []).map((r, i) => {
    const p = pts[i];
    if (!p) return '';
    if (r === null) return `${esc(p.name)}: …`;
    if (r === 'no route') return `${esc(p.name)}: no route`;
    const kind = {TRAM: 'Tram ', BUS: 'Bus ', TROLLEYBUS: 'Bus ', INTERCITY_BUS: 'Bus ', FERRY: 'Boat ', FUNICULAR: 'Funicular '};
    const rides = (r.rides || []).map(x => (kind[x.type] || '') + x.line).join(' → ');
    const tip = `to ${r.to}${rides ? ': ' + rides : ''}` + (r.night_min != null ? `\nnight return from ${p.return_at}: home after ${r.night_min} min` : '');
    return `<span title="${esc(tip)}">${esc(p.name)}: ${r.min}′` + (p.mode === 'transit' ? ` · ${r.transfers}× · ${esc(rides)}` : ` · ${r.km} km`)
      + (r.night_min != null ? ` · 🌙${r.night_min}′` : '') + '</span>';
  }).filter(Boolean).join('<br>');
}
// A value contradicted by the listing's text or the other portal: shown with ⚠, never used to exclude.
function val(l, f, show = esc) {
  const c = (l.conflicts || {})[f];
  return c ? `<span title="${esc(c)}" style="cursor: help">${show(l[f])} ⚠</span>` : show(l[f]);
}
// floor in German convention (EG, 2. Stock...); "from text" when only the description gives it
const floorOf = l => l.floor ? val(l, 'floor') + (l.floor_source === 'text' ? ' <span class="muted" title="from the description">(text)</span>' : '') : '–';
const addressOf = l => [l.address, [l.zip, l.city].filter(Boolean).join(' ')].filter(Boolean).join(', ');
const statusPill = s => `<span class="st ${stClass(s)}">${stLabel(s)}</span>`;
const taxOn = () => data.state.tax.income > 0;
// pinned photos first (in the order you pinned them), then the rest; pins of photos no longer listed are ignored
function orderedImages(l) {
  const imgs = l.images || [], pins = (user(l).pinned || []).filter(p => imgs.includes(p));
  return [...pins, ...imgs.filter(i => !pins.includes(i))];
}
const img = (src, attrs = '') => `<img src="${esc(src)}" referrerpolicy="no-referrer" alt="" ${attrs}>`;

// The list is only rendered while the listings tab is shown (setTab renders it when you come back if it changed),
// and cards / table rows are added in chunks as you scroll: a few thousand at once take seconds to lay out.
let listStale = true, shown = [], rendered = 0;
const CHUNK = 60;
function renderList(fromTop = false) {
  if (!data) return;
  if (tab !== 'listings') { listStale = true; return; }
  listStale = false;
  document.body.dataset.view = view;
  document.querySelectorAll('#viewSeg button').forEach(b => b.classList.toggle('on', b.dataset.view === view));
  shown = rows();
  renderFilterBar(shown);
  if (view === 'map') return renderMap();
  // keep as many items as were there, so a re-render (new status, a poll) doesn't cut the list above where you are
  const n = fromTop ? CHUNK : Math.max(CHUNK, rendered);
  rendered = 0;
  if (view === 'cards') renderCards(); else renderTable();
  more(n);
}
// Adds items up to index n (at least one more chunk); the observer calls it when the end of the list scrolls near.
function more(n = rendered + CHUNK) {
  if (view === 'map' || rendered >= shown.length) return;
  n = Math.min(shown.length, Math.max(n, rendered + CHUNK));
  const items = shown.slice(rendered, n);
  if (view === 'cards') $('cards').insertAdjacentHTML('beforeend', items.map(card).join(''));
  else $('rows').insertAdjacentHTML('beforeend', items.map(tableRow).join(''));
  rendered = n;
  moreObserver.unobserve($('more')); moreObserver.observe($('more'));  // re-check: still near the end after adding?
}
const moreObserver = new IntersectionObserver(es => es.some(e => e.isIntersecting) && more(),
  {root: $('listpane'), rootMargin: '0px 0px 1500px 0px'});

function renderFilterBar(rs) {
  const all = Object.entries(data.listings), base = all.filter(r => matches(r, true));
  const n = s => base.filter(([, l]) => s === 'all' || (s === 'starred' ? user(l).starred : (user(l).status || 'inbox') === s)).length;
  const segs = [['all', 'All'], ['inbox', 'Inbox'], ...ST.slice(1), ['starred', '★ Starred']];
  $('statusFilter').innerHTML = (STATIC ? segs.filter(([s]) => ['all', 'maybe', 'shortlist', 'starred'].includes(s)) : segs).map(([s, label]) =>
    `<button data-s="${s}" class="${filters.status === s ? 'on' : ''}">${label}<span class="n">${n(s)}</span></button>`).join('');
  const tags = allTags();
  if (filters.tag && !tags.includes(filters.tag)) tags.unshift(filters.tag);
  $('tagFilter').innerHTML = `<option value="">All tags</option>` + tags.map(t => `<option${t === filters.tag ? ' selected' : ''}>${esc(t)}</option>`).join('');
  $('tagFilter').hidden = !tags.length;
  $('f-unseen').checked = filters.unseen; $('showExcluded').checked = filters.excluded;
  const kinds = {};
  for (const [, l] of all) if (l.excluded) kinds[exclusionKind(l.excluded)] = (kinds[exclusionKind(l.excluded)] || 0) + 1;
  $('reasonFilter').innerHTML = `<option value="">All reasons</option>` + EXCLUSION_KINDS.filter(([k]) => kinds[k] || k === filters.reason)
    .map(([k, label]) => `<option value="${k}"${k === filters.reason ? ' selected' : ''}>${label} (${kinds[k] || 0})</option>`).join('');
  $('reasonFilter').hidden = !filters.excluded;
  const nRemoved = all.filter(([, l]) => l.deleted_at && l.excluded !== 'hidden').length;
  $('removedFilter').innerHTML = [['hide', `Hide removed (${nRemoved})`], ['show', 'Show removed'], ['only', `Only removed (${nRemoved})`]]
    .map(([v, label]) => `<option value="${v}"${v === filters.removed ? ' selected' : ''}>${label}</option>`).join('');
  $('removedFilter').hidden = !nRemoved && filters.removed !== 'only';
  const opts = {...SORTS, ...(SORTS[sortKey] ? {} : {[sortKey]: document.querySelector(`th[data-k="${sortKey}"]`)?.textContent || sortKey})};
  $('sortSel').innerHTML = Object.entries(opts).map(([k, label]) => `<option value="${k}"${k === sortKey ? ' selected' : ''}>${esc(label)}</option>`).join('');
  $('sortDir').textContent = sortDir > 0 ? '↑' : '↓';
  const eligible = all.filter(([, l]) => !l.excluded).length, unseen = rs.filter(([, l]) => !user(l).seen).length;
  $('counts').textContent = STATIC ? `${rs.length} of ${all.length} flats` : `${rs.length} shown · ${eligible} eligible · ${all.length} total`;
  $('markSeen').hidden = !unseen;
  $('markSeen').textContent = `Mark ${unseen} shown as opened`;
  const base0 = all.map(([, l]) => l).find(l => l.tax_extra_month_chf === 0 && !GONE.includes(l.excluded));
  $('taxBase').textContent = base0 ? `Tax baseline: ${base0.tax_commune}, ${base0.tax_year_chf} CHF/yr` : '';
  const stale = conflictKeys();
  $('refreshConflicts').hidden = !stale.length;
  $('refreshConflicts').textContent = `↻ Re-check ${stale.length} listing(s) with conflicting data`;
}

// ---------- cards ----------

function card([k, l]) {
  const u = user(l), cover = orderedImages(l)[0];
  return `<article class="card${k === selected ? ' sel' : ''}${l.excluded ? ' excluded' : ''}" data-key="${esc(k)}">
    <div class="thumb">${cover ? img(cover, 'loading="lazy"') : '<div class="noimg">no photos</div>'}
      <div class="tl">${u.seen ? '' : '<span class="newdot" title="You haven\'t opened this one yet">new</span>'}${u.status ? statusPill(u.status) : ''}</div>
      <button class="star${u.starred ? ' on' : ''}" data-act="star" title="Star (s)">★</button>
      ${(l.images || []).length ? `<span class="imgcount">${l.images.length} photo${l.images.length > 1 ? 's' : ''}</span>` : ''}
    </div>
    <div class="body">
      <div class="price">CHF ${val(l, 'rent_chf', fmt)}${taxOn() && l.rent_tax_adjusted_chf != null ? ` <span class="muted">· ${fmt(l.rent_tax_adjusted_chf)} tax-adj.</span>` : ''}</div>
      <div class="facts">${val(l, 'rooms', fmt)} rooms · ${val(l, 'area_m2', fmt)} m²${l.floor ? ` · ${floorOf(l)}` : ''}${l.available_from ? ` · from ${esc(l.available_from)}` : ''}</div>
      <div class="addr" title="${esc(addressOf(l))}">${esc(addressOf(l) || l.title)}</div>
      <div>${(u.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join('')}${aiBadges(l.ai, true)}</div>
      ${u.note ? `<div class="notesnip" title="${esc(u.note)}">📝 ${esc(u.note.split('\n')[0])}</div>` : ''}
      ${l.deleted_at ? `<div class="excl">${removedText(l)}</div>` : l.excluded ? `<div class="excl">${esc(l.excluded)}</div>` : ''}
    </div></article>`;
}
function renderCards() {
  $('cards').innerHTML = shown.length ? '' : `<div class="empty">No listings match.${Object.keys(data.listings).length ? '' : ' Run a scrape to get started.'}</div>`;
}

// ---------- table ----------

function renderTable() {
  document.querySelectorAll('th[data-k]').forEach(th => th.dataset.arrow = th.dataset.k === sortKey ? (sortDir > 0 ? ' ▲' : ' ▼') : '');
  $('rows').innerHTML = '';
}
function tableRow([k, l]) {
  const u = user(l);
  return `<tr data-key="${esc(k)}" class="${l.excluded ? 'excluded' : ''}${k === selected ? ' sel' : ''}">
    <td>${statusPill(u.status)}${u.starred ? ' ★' : ''}${u.seen ? '' : ' <span class="newdot">new</span>'}</td>
    <td>${esc(l.site)}</td>
    <td>${l.provider_logo ? `<img src="${esc(l.provider_logo)}" title="${esc(l.provider_label)}"><br>` : ''}${esc(l.provider || l.provider_label)}</td>
    <td><a href="${esc(l.url)}" target="_blank" rel="noreferrer">${esc(l.title || l.id)}</a>${l.ai && l.ai.summary ? `<div class="summary">${esc(l.ai.summary)}</div>` : ''}
      ${(u.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join('')}</td>
    <td>${val(l, 'rooms')}</td><td>${val(l, 'area_m2')}</td><td>${l.floor ? floorOf(l) : ''}</td><td>${val(l, 'rent_chf')}</td>
    <td title="${esc(l.tax_commune)}: ${esc(l.tax_year_chf)} CHF/yr">${esc(l.tax_extra_month_chf)}</td><td><b>${esc(l.rent_tax_adjusted_chf)}</b></td>
    <td>${esc(addressOf(l))}</td><td>${esc(l.available_from)}${l.available_source === 'AI' ? ' <span class="muted">AI</span>' : ''}</td>
    <td>${esc((l.published || l.first_seen || '').slice(0, 10))}</td>${kitchenCell(l.ai)}<td>${aiBadges(l.ai)}</td><td class="muted" style="white-space: nowrap">${commuteCell(l.commute)}</td>
    <td>${l.deleted_at ? removedText(l) : esc(l.excluded)}</td>
  </tr>`;
}
// Show/hide columns: CSS nth-child rules, remembered per browser.
let hiddenCols = ls('hiddenCols', []);
function renderColumns() {
  const ths = [...document.querySelectorAll('#results th')], name = th => th.textContent.trim();
  $('colToggles').innerHTML = ths.map(th => `<label><input type="checkbox" data-col="${esc(name(th))}"
    ${hiddenCols.includes(name(th)) ? '' : 'checked'} onchange="toggleColumn(this)"> ${esc(name(th))}</label>`).join('');
  $('colStyle').textContent = ths.map((th, i) => hiddenCols.includes(name(th))
    ? `#results th:nth-child(${i + 1}), #results td:nth-child(${i + 1}) { display: none; }` : '').join('\n');
}
function toggleColumn(cb) {
  hiddenCols = cb.checked ? hiddenCols.filter(c => c !== cb.dataset.col) : [...hiddenCols, cb.dataset.col];
  lsSet('hiddenCols', hiddenCols); renderColumns();
}

// ---------- map (Leaflet + OpenStreetMap) ----------

let map, markerLayer, areaLayer, areaRenderer, areasFor, markers = {}, mapFitted = false;
const cssVar = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
function markerStyle(k, l) {
  const sel = k === selected, u = user(l);
  return {radius: sel ? 11 : u.starred ? 8.5 : 6.5, weight: sel ? 3 : u.starred ? 3 : 1.5,
    color: sel ? cssVar('--text') : u.starred ? '#f59e0b' : '#fff',
    fillColor: cssVar('--' + (l.excluded ? 'st-excluded' : stClass(u.status))), fillOpacity: l.excluded ? .55 : .92};
}
function renderMap() {
  if (!window.L) { $('map').innerHTML = '<div class="empty">The map library could not be loaded (offline?).</div>'; return; }
  if (!map) {
    map = L.map('map', {preferCanvas: true}).setView([47.37, 8.54], 11);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
    markerLayer = L.layerGroup().addTo(map);
    initAreas();
  }
  if (areasFor !== data.state) drawAreas();  // first render, or settings reloaded after a conflict
  map.invalidateSize();
  markerLayer.clearLayers(); markers = {};
  const rs = shown, pts = [];
  for (const [k, l] of rs) {
    if (l.lat == null || l.lon == null) continue;
    const cover = orderedImages(l)[0];
    markers[k] = L.circleMarker([l.lat, l.lon], {...markerStyle(k, l), pmIgnore: true}).addTo(markerLayer)
      .bindTooltip(`${cover ? img(cover) : ''}<div><b>CHF ${fmt(l.rent_chf)}</b> · ${fmt(l.rooms)} rooms · ${fmt(l.area_m2)} m²${l.floor ? ` · ${esc(l.floor)}` : ''}<br>
        ${esc(addressOf(l))}${user(l).status ? '<br>' + statusPill(user(l).status) : ''}</div>`, {className: 'maptip', direction: 'top', offset: [0, -8]})
      .on('click', () => select(k));
    pts.push([l.lat, l.lon]);
  }
  if (markers[selected]) markers[selected].bringToFront();
  if (!mapFitted && pts.length) { map.fitBounds(pts, {padding: [40, 40], maxZoom: 15}); mapFitted = true; }
  const missing = rs.length - pts.length;
  $('mapLegend').innerHTML = [...ST, ['excluded', 'Excluded']].map(([s, label]) =>
    `<span><i style="--c: var(--${s === 'excluded' ? 'st-excluded' : stClass(s)})"></i>${label}</span>`).join('')
    + `<span><i style="--c: transparent; border: 2px solid #f59e0b"></i>Starred</span>`
    + (missing ? `<span class="muted">${missing} without location</span>` : '');
  renderAreaPanel();
}

// ---------- exclusion areas: drawn on the map (Leaflet-Geoman), saved in state.exclusion_areas ----------
// app.py excludes listings inside an area that's on ("area: name"), before commute and AI triage, so those skip them.

const areas = () => data.state.exclusion_areas || (data.state.exclusion_areas = []);
const AREA_STYLE = {color: '#dc2626', weight: 2, dashArray: '6 4', fillColor: '#dc2626', fillOpacity: .12};
const areaStyle = a => a.on === false ? {...AREA_STYLE, opacity: .45, fillOpacity: 0} : AREA_STYLE;
let areaLayers = {};  // area id -> Leaflet layer
function inPolygon(lat, lon, pts) {  // same ray casting as scrape.in_polygon
  let inside = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [y1, x1] = pts[i], [y2, x2] = pts[j];
    if ((y1 > lat) !== (y2 > lat) && lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1)) inside = !inside;
  }
  return inside;
}
// Listings an area hides: inside it and not excluded for another reason (the server labels a listing with its first area).
const areaHides = a => Object.values(data.listings).filter(l => l.lat != null && l.lon != null
  && (!l.excluded || l.excluded.startsWith('area: ')) && inPolygon(l.lat, l.lon, a.points)).length;
const layerPoints = layer => layer.getLatLngs()[0].map(p => [+p.lat.toFixed(6), +p.lng.toFixed(6)]);

function initAreas() {
  if (!map.pm || STATIC) return;  // Geoman didn't load: areas are still drawn and applied, just not editable
  // SVG (Geoman drags SVG paths) in a pane above the markers' canvas, which would swallow the clicks otherwise;
  // areas only take clicks while editing, dragging or removing, so listings inside them stay clickable
  map.createPane('areas').style.zIndex = 450;
  map.on('pm:globaleditmodetoggled pm:globaldragmodetoggled pm:globalremovalmodetoggled', () =>
    map.getContainer().classList.toggle('areas-live', map.pm.globalEditModeEnabled() || map.pm.globalDragModeEnabled()
      || map.pm.globalRemovalModeEnabled()));
  areaRenderer = L.svg({pane: 'areas'});
  map.pm.setGlobalOptions({pathOptions: {...AREA_STYLE, renderer: areaRenderer, pane: 'areas'}, snappable: false});
  map.pm.addControls({position: 'topleft', drawMarker: false, drawCircleMarker: false, drawPolyline: false, drawCircle: false,
    drawText: false, cutPolygon: false, rotateMode: false});
  map.on('pm:create', ({layer, shape}) => {
    const a = {id: Date.now().toString(36), name: `Area ${areas().length + 1}`, shape: shape === 'Rectangle' ? 'rectangle' : 'polygon',
      on: true, points: layerPoints(layer)};
    layer.remove();  // redrawn by drawAreas with its handlers
    areas().push(a);
    drawAreas(); saveAreas();
  });
}
function drawAreas() {
  areasFor = data.state;
  if (!areaLayer) areaLayer = L.layerGroup().addTo(map);
  areaLayer.clearLayers(); areaLayers = {};
  for (const a of areas()) {
    const opts = {...areaStyle(a), renderer: areaRenderer, pane: areaRenderer ? 'areas' : 'overlayPane'};
    const layer = (a.shape === 'rectangle' ? L.rectangle(L.latLngBounds(a.points), opts) : L.polygon(a.points, opts)).addTo(areaLayer);
    layer.bindTooltip(() => `<b>${esc(a.name)}</b><br>${a.on === false ? 'off' : `hides ${areaHides(a)}`}`, {sticky: true});
    // pm:edit fires after a vertex edit and after a drag
    layer.on('pm:edit', () => { a.points = layerPoints(layer); saveAreas(); });
    layer.on('pm:remove', () => removeArea(a.id, false));
    areaLayers[a.id] = layer;
  }
  renderAreaPanel();
}
// Saves are debounced: dragging a vertex around fires many edits.
let areaTimer;
function saveAreas() {
  renderAreaPanel();
  clearTimeout(areaTimer);
  areaTimer = setTimeout(() => saveState(), 400);
}
function removeArea(id, fromPanel = true) {
  data.state.exclusion_areas = areas().filter(a => a.id !== id);
  if (fromPanel) drawAreas();
  saveAreas();
}
function setArea(id, patch) {
  Object.assign(areas().find(a => a.id === id), patch);
  if ('on' in patch) areaLayers[id]?.setStyle(areaStyle(areas().find(a => a.id === id)));
  saveAreas();
}
function zoomArea(id) { const l = areaLayers[id]; l && map.fitBounds(l.getBounds(), {padding: [40, 40]}); }
function renderAreaPanel() {
  const el = $('mapAreas');
  if (!el || !data) return;
  const list = areas();
  el.innerHTML = `<b>Excluded areas</b>` + (list.length ? list.map(a => `<div class="arearow${a.on === false ? ' off' : ''}">
      <input type="checkbox" ${a.on === false ? '' : 'checked'} title="Exclude the listings in this area"
        onchange="setArea('${a.id}', {on: this.checked})">
      <input class="areaname" value="${esc(a.name)}" onchange="setArea('${a.id}', {name: this.value.trim() || 'Area'})">
      <span class="muted">${a.on === false ? 'off' : `hides ${areaHides(a)}`}</span>
      <button class="ghost icon" onclick="zoomArea('${a.id}')" title="Zoom to this area">⌖</button>
      <button class="ghost icon" onclick="removeArea('${a.id}')" title="Delete this area">×</button></div>`).join('')
    : `<div class="muted">${map && map.pm ? 'Draw a rectangle or polygon with the tools on the left to hide the listings in it.'
      : 'The drawing tools could not be loaded (offline?).'}</div>`)
    + (list.length && map && map.pm ? `<div class="muted hint">Edit corners or drag areas with the tools on the left.</div>` : '');
}
function showOnMap() {
  const l = cur();
  if (!l || l.lat == null) return;
  setView('map');
  map && map.setView([l.lat, l.lon], 16);
}

// ---------- detail panel ----------

function select(k, scroll = true) {
  if (!data.listings[k]) return;
  flushNote();
  if (k !== selected) imgIdx = 0;
  const prev = selected;
  selected = k; lsSet('selected', k);
  if (!user(data.listings[k]).seen) setUser(k, {seen: new Date().toISOString().slice(0, 19)}, false);
  renderDetail();
  const i = shown.findIndex(([key]) => key === k);
  if (i >= rendered) more(i + 1);  // j/k past the rendered part of the list
  // restyle the old and new selection in place: re-rendering the list would lose its scroll position
  document.querySelectorAll('#cards .sel, #rows .sel').forEach(e => e.classList.remove('sel'));
  for (const e of document.querySelectorAll(`[data-key="${CSS.escape(k)}"]`)) {
    e.classList.add('sel');
    e.querySelector('.newdot')?.remove();
    if (scroll) e.scrollIntoView({block: 'nearest'});
  }
  if (map && view === 'map') {
    for (const key of [prev, k]) if (markers[key]) markers[key].setStyle(markerStyle(key, data.listings[key]));
    const m = markers[k];
    if (m) { m.bringToFront(); if (scroll && !map.getBounds().contains(m.getLatLng())) map.panTo(m.getLatLng()); }
  }
  if (!$('lightbox').hidden) orderedImages(cur()).length ? renderLightbox() : closeLightbox();
}
function move(d) {
  const ks = rows().map(([k]) => k);
  if (!ks.length) return;
  const i = ks.indexOf(selected);
  select(ks[i < 0 ? 0 : Math.max(0, Math.min(ks.length - 1, i + d))]);
}
function hideSelected() {
  if (!cur() || STATIC) return;
  const ks = rows().map(([k]) => k), i = ks.indexOf(selected), next = ks[i + 1] || ks[i - 1], k = selected;
  if (cur().excluded === 'hidden') return unhide(k);
  hide(k);
  if (next && !filters.excluded) select(next);
}

function renderDetail() {
  const el = $('detail'), l = cur();
  if (!l) {
    el.innerHTML = `<div class="d-empty">Pick a listing to see its photos, details and your notes.<br><br>
      <kbd>j</kbd> / <kbd>k</kbd> move through the list, <kbd>?</kbd> shows all shortcuts.</div>`;
    return;
  }
  const u = user(l), k = selected, tags = u.tags || [];
  const provider = l.provider ? esc(l.provider) : l.provider_key
    ? `<span class="provider-row"><input id="provLabel" value="${esc(l.provider_label)}" placeholder="name this logo + Enter"
        title="Name this logo's provider (applies to all its listings)" onkeydown="if (event.key === 'Enter') setLabel('${esc(l.provider_key)}', this.value)"></span>` : '';
  const blocked = (l.excluded || '').startsWith('blocked: ');
  const fact = (label, v) => `<div><b>${v}</b><span>${label}</span></div>`;
  el.innerHTML = `
  <div id="dGallery"></div>
  <div class="d-sec">
    <div class="d-title"><h2><a href="${esc(l.url)}" target="_blank" rel="noreferrer" title="Open on ${esc(l.site)} (o)">${esc(l.title || l.id)}</a></h2>
      <a href="${esc(l.url)}" target="_blank" rel="noreferrer">Open ↗</a></div>
    <div class="d-meta"><span>${esc(l.site)}</span>
      ${l.provider_logo ? img(l.provider_logo, `title="${esc(l.provider_label)}"`) : ''}${provider}
      <span>seen since ${esc((l.first_seen || '').slice(0, 10))}</span>
      ${(l.also_on || []).length ? `<span title="The same flat on other portals: your notes, hiding and blocking apply to all copies">also on
        ${l.also_on.map(a => `<a href="${esc(a.url)}" target="_blank" rel="noreferrer">${esc(a.site)} ↗</a>`).join(' ')}</span>` : ''}
      ${l.deleted_at ? `<span class="excl">${removedText(l)}</span>` : l.excluded ? `<span class="excl">excluded: ${esc(l.excluded)}</span>` : ''}</div>
  </div>
  <div class="d-sec">
    <div class="statusbtns">${ST.map(([s, label], i) =>
      `<button class="${stClass(s)}${(u.status || '') === s ? ' on' : ''}" onclick="setStatus(selected, '${s}')" title="${label} (${i})">${label}</button>`).join('')}</div>
    <div class="triagerow">
      <button class="star${u.starred ? ' on' : ''}" onclick="toggleStar(selected)" title="Star (s)">★</button>
      ${tags.map(t => `<span class="tag">${esc(t)}<button onclick="removeTag(this.dataset.t)" data-t="${esc(t)}" title="remove tag">×</button></span>`).join('')}
      <input id="tagInput" list="tagList" placeholder="+ tag (t)" onkeydown="if (event.key === 'Enter') { addTag(this.value); this.value = ''; }">
      <datalist id="tagList">${allTags().filter(t => !tags.includes(t)).map(t => `<option value="${esc(t)}">`).join('')}</datalist>
    </div>
  </div>
  <div class="d-sec"${STATIC && !u.note ? ' hidden' : ''}>
    <h4>Notes <span id="noteState">${u.updated ? 'last edited ' + esc(u.updated.slice(0, 10)) : ''}</span></h4>
    <textarea id="noteBox" data-key="${esc(k)}" placeholder="Your notes: questions for the agency, viewing impressions… (n)"
      oninput="noteInput()" onblur="flushNote()"${STATIC ? ' readonly' : ''}>${esc(u.note)}</textarea>
  </div>
  <div class="d-sec">
    <div class="d-facts">
      ${fact('rent CHF/mo', val(l, 'rent_chf', fmt))}
      ${taxOn() ? fact(`tax-adjusted (${l.tax_extra_month_chf != null ? '+' + fmt(l.tax_extra_month_chf) : '?'} tax)`, fmt(l.rent_tax_adjusted_chf)) : ''}
      ${fact('rooms', val(l, 'rooms', fmt))}${fact('m²', val(l, 'area_m2', fmt))}${fact('floor', floorOf(l))}${fact('CHF per m²', fmt(chfM2(l)))}
      ${fact('available' + (l.available_source === 'AI' ? ' (AI)' : ''), esc(l.available_from || '–'))}
      ${fact('published', esc((l.published || '').slice(0, 10) || '–'))}
      ${l.ai ? fact('kitchen', `<span class="k-${esc(l.ai.kitchen)}" title="${esc(l.ai.evidence.kitchen || '')}">${esc(l.ai.kitchen)}</span>`) : ''}
    </div>
    <p>${esc(addressOf(l))} ${l.lat != null ? '<button class="link" onclick="showOnMap()">show on map</button>' : ''}</p>
    ${l.commute && l.commute.length ? `<div class="muted">${commuteCell(l.commute)}</div>` : ''}
  </div>
  <div class="d-sec">
    <h4>AI triage</h4>
    ${l.ai && l.ai.summary ? `<p>${esc(l.ai.summary)}</p>` : ''}
    <div>${aiBadges(l.ai)}</div>
  </div>
  ${l.description ? `<div class="d-sec"><h4>Description</h4><div class="desc${isHtml(l.description) ? ' html' : ''}" onclick="this.classList.toggle('open')" title="click to expand">${descHtml(l.description)}</div></div>` : ''}
  <div class="d-sec d-actions">
    <button class="${l.excluded === 'hidden' ? '' : 'danger'}" onclick="hideSelected()" title="Hide (h): excluded from eligible.json; next listing">${l.excluded === 'hidden' ? 'Unhide' : 'Hide (reject)'}</button>
    ${l.site !== 'newhome' ? `<button onclick="refreshListings(['${esc(k)}'])" title="Re-read from the listing page${l.source === 'page' ? ' (already verified)' : ''}">↻ Re-read</button>` : ''}
    ${blocked ? `<button onclick="unblock(this.dataset.t)" data-t="${esc(l.excluded.slice(9))}">Unblock ${esc(l.excluded.slice(9))}</button>`
      : l.provider || l.provider_key ? `<button class="danger" onclick="blockProvider('${esc(l.provider_key || '')}')">Block provider</button>` : ''}
  </div>`;
  renderGallery();
}

function renderGallery() {
  const l = cur(), g = $('dGallery');
  if (!l || !g) return;
  const imgs = orderedImages(l), pins = new Set(user(l).pinned || []);
  imgIdx = Math.max(0, Math.min(imgIdx, imgs.length - 1));
  if (!imgs.length) { g.innerHTML = ''; return; }
  const pinned = pins.has(imgs[imgIdx]), many = imgs.length > 1;
  g.innerHTML = `<div class="gallery">
      ${img(imgs[imgIdx], 'onclick="openLightbox()" title="Full screen (f)"')}
      ${many ? '<button class="nav prev" onclick="step(-1)" title="Previous (←)">‹</button><button class="nav next" onclick="step(1)" title="Next (→)">›</button>' : ''}
      <div class="over"><span>${imgIdx + 1} / ${imgs.length}</span>
        <button class="${pinned ? 'on' : ''}" onclick="togglePin()" title="Pinned photos come first and become the cover (p)">📌 ${pinned ? 'Pinned' : 'Pin'}</button>
        <button onclick="openLightbox()">⤢ Full screen</button></div>
    </div>
    ${many ? `<div class="strip">${imgs.map((src, i) => `<span class="pinwrap${pins.has(src) ? ' pinned' : ''}">${img(src, `loading="lazy" class="${i === imgIdx ? 'cur' : ''}" onclick="showImg(${i})"`)}</span>`).join('')}</div>` : ''}`;
  g.querySelector('.strip .cur')?.scrollIntoView({block: 'nearest', inline: 'nearest'});
}
function showImg(i) { imgIdx = i; renderGallery(); if (!$('lightbox').hidden) renderLightbox(); }
function step(d) {
  const n = cur() ? orderedImages(cur()).length : 0;
  if (n) showImg((imgIdx + d + n) % n);
}
function togglePin() {
  const l = cur(), src = l && orderedImages(l)[imgIdx];
  if (!src) return;
  const pins = user(l).pinned || [];
  setUser(selected, {pinned: pins.includes(src) ? pins.filter(p => p !== src) : [...pins, src]}, false);
  imgIdx = orderedImages(l).indexOf(src);  // keep showing the same photo, wherever it moved
  renderGallery(); renderList();
  if (!$('lightbox').hidden) renderLightbox();
}

function openLightbox() {
  if (!cur() || !orderedImages(cur()).length) return;
  $('lightbox').hidden = false; renderLightbox();
}
function closeLightbox() { $('lightbox').hidden = true; }
function renderLightbox() {
  const l = cur(), imgs = orderedImages(l), pins = new Set(user(l).pinned || []), src = imgs[imgIdx];
  $('lbImg').src = src; $('lbOpen').href = src;
  $('lbCount').textContent = `${imgIdx + 1} / ${imgs.length} · ${l.title || ''}`;
  $('lbPin').textContent = pins.has(src) ? '📌 Pinned' : '📌 Pin';
  $('lbPin').classList.toggle('on', pins.has(src));
  $('lbThumbs').innerHTML = imgs.map((s, i) => img(s, `loading="lazy" class="${i === imgIdx ? 'cur' : ''}${pins.has(s) ? ' pinned' : ''}" onclick="showImg(${i})"`)).join('');
  $('lbThumbs').querySelector('.cur')?.scrollIntoView({block: 'nearest', inline: 'center'});
}

// ---------- settings bits ----------

let models = {};
async function loadModels() {  // the key's available models, fetched once per page load / key change
  models = await (await fetch('/api/models')).json();
  showKeyStatus();
}
function showKeyStatus(fromState = false) {
  $('keystatus-google_maps').textContent = data.keys.google_maps ? '✓' : '✗ no key';
  for (const p of ['anthropic', 'openai']) {
    $('keystatus-' + p).textContent = data.keys[p] ? '✓' : '✗ no key';
    // keep what's selected on the page (maybe unsaved); the saved choice only when filling the form
    const cur = fromState ? data.state.triage.models[p] : $('model-' + p).value, def = data.default_models[p];
    const ids = [...new Set([...(models[p] || []), ...(cur ? [cur] : [])])].filter(m => m !== def);
    $('model-' + p).innerHTML = `<option value="">${esc(def)} (default)</option>` +
      ids.map(m => `<option>${esc(m)}</option>`).join('');
    $('model-' + p).value = cur;
  }
}
async function saveKey(p) {
  const key = $('key-' + p).value.trim();
  if (!key) return;
  await post('/api/secrets', {provider: p, key});
  $('key-' + p).value = ''; await refresh(); loadModels();
}

function pointRow(p) {
  const v = x => x == null || x === 0 ? '' : esc(x);
  return `<div class="point">
    <input class="p-name" size="8" value="${esc(p.name)}" title="name">
    <select class="p-mode">${['transit', 'drive', 'bicycle', 'walk'].map(m => `<option${m === p.mode ? ' selected' : ''}>${m}</option>`).join('')}</select>
    <button onclick="this.closest('.point').remove()" title="remove destination">×</button>
    <textarea class="p-addresses" rows="2" placeholder="one address per line; the best route to any of them counts&#10;e.g. Zürich HB&#10;Opernhaus, Zürich">${esc((p.addresses || p.address || ''))}</textarea>
    arrive by <input class="p-arrive_by" type="time" value="${esc(p.arrive_by)}">
    max <input class="p-max_minutes" type="number" min="0" value="${v(p.max_minutes)}"> min
    <input class="p-max_km" type="number" min="0" value="${v(p.max_km)}"> km<br>
    <span class="muted">transit:</span> max <input class="p-max_transfers" type="number" min="0" value="${p.max_transfers ?? ''}"> transfers,
    <label style="display: inline"><input type="checkbox" class="p-free_train_transfers"${p.free_train_transfers ? ' checked' : ''}> train↔train free</label><br>
    <span class="muted">night return:</span> leave at <input class="p-return_at" type="time" value="${esc(p.return_at || '')}">,
    home within <input class="p-max_night_minutes" type="number" min="0" value="${v(p.max_night_minutes)}"> min
  </div>`;
}
function renderPoints() {
  $('points').innerHTML = data.state.commute.points.map(pointRow).join('');
  $('commute-auto').checked = data.state.commute.auto;
}
function addPoint() {
  $('points').insertAdjacentHTML('beforeend', pointRow({name: 'Work', addresses: '', mode: 'transit', arrive_by: '08:30', max_minutes: 45,
    max_transfers: 1, free_train_transfers: true, return_at: '', max_night_minutes: 60}));
}

function conflictKeys() {  // homegate copy only (refreshing it also updates the immoscout24 twin), current listings
  const seen = new Set();
  return Object.entries(data.listings).filter(([, l]) => Object.keys(l.conflicts || {}).length && l.site !== 'newhome'
    && !['deleted', 'other search'].includes(l.excluded) && !seen.has(l.id) && seen.add(l.id)).map(([k]) => k);
}
async function refreshListings(keys) {
  await post('/api/refresh', {keys}); poll();
}

// Same as scrape.blocked_by: a term blocks by itself or by its name without company form/country ("Apleona Schweiz AG": "apleona").
const SUFFIXES = new Set(['ag', 'sa', 'gmbh', 'sàrl', 'sarl', 'ltd', 'inc', 'kg', 'co', '&', 'schweiz', 'suisse', 'svizzera',
  'switzerland', 'swiss', 'ch', 'real', 'estate', 'immobilien', 'immobilier', 'management', 'services', 'group', 'holding']);
function blockCore(term) {
  const words = term.trim().toLowerCase().replace(/\.$/, '').split(/\s+/);
  while (words.length > 1 && SUFFIXES.has(words.at(-1).replace(/[.,]/g, ''))) words.pop();
  const core = words.join(' ').replace(/^[.,]+|[.,]+$/g, '');
  return core.length >= 3 ? core : term.trim().toLowerCase();
}
function blockMatch(text, term) {
  text = text.toLowerCase();
  if (!term.trim()) return false;
  const core = blockCore(term).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return text.includes(term.trim().toLowerCase()) || new RegExp(`(?<![\\p{L}\\p{N}_])${core}(?![\\p{L}\\p{N}_])`, 'u').test(text);
}

function renderProviders() {
  const logos = {}, counts = {};
  for (const l of Object.values(data.listings)) if (l.provider_key) {
    logos[l.provider_key] = l.provider_logo;
    if (!GONE.includes(l.excluded)) counts[l.provider_key] = (counts[l.provider_key] || 0) + 1;
  }
  const term = label => data.state.blocklist.find(b => blockMatch(label, b)), blocked = label => term(label) !== undefined;
  const rows = Object.entries(data.state.provider_labels).sort(([, a], [, b]) => a.localeCompare(b)).map(([key, label]) =>
    `<div class="provider ${blocked(label) ? 'blocked' : ''}">${logos[key] ? `<img src="${esc(logos[key])}">` : ''}
      ${esc(label)} (${counts[key] || 0}) ${blocked(label) ? '<b>blocked</b>' : ''}
      ${blocked(label) ? `<button onclick="unblock(this.dataset.t)" data-t="${esc(term(label))}">unblock</button>`
        : `<button onclick="blockLabel(this.dataset.t)" data-t="${esc(label)}">block</button>`}
      <button onclick="setLabel('${esc(key)}', '')" title="Remove label">forget</button></div>`);
  $('providers').innerHTML = rows.join('') || '<span class="muted">None yet: name a logo in a listing\'s detail panel.</span>';
}

function renderLocations() {
  $('locations').innerHTML = data.state.search.locations.map((l, i) =>
    `<span class="chip" title="${esc(['homegate', 'immoscout24', 'newhome'].map(k => k + ': ' + (l[k] || '—')).join('\n'))}">${esc(l.label)}
     <button onclick="data.state.search.locations.splice(${i}, 1); renderLocations()">×</button></span>`).join('');
}
let suggestTimer, suggestions = [];
function suggest(addFirst) {
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(async () => {
    const q = $('locq').value;
    suggestions = q.trim() ? await (await fetch('/api/locations?q=' + encodeURIComponent(q))).json() : [];
    $('suggestions').innerHTML = suggestions.length ? suggestions.map((l, i) => `<button onclick="addLocation(${i})">${esc(l.label)}</button>`).join(' ')
      : q.trim() ? '<span class="muted">no match</span>' : '';
    if (addFirst && suggestions.length) addLocation(0);
  }, addFirst ? 0 : 250);
}
function addLocation(i) {
  const l = suggestions[i];
  if (!data.state.search.locations.some(x => x.id === l.id)) data.state.search.locations.push(l);
  $('locq').value = ''; $('suggestions').innerHTML = ''; renderLocations();
}

// ---------- runs ----------

function showRun(run) {
  $('log').textContent = run.log.join('\n');
  $('log').scrollTop = 1e9;
  const last = run.log[run.log.length - 1] || '';
  $('runpill').className = 'pill' + (run.running ? ' running' : '');
  $('runpill').textContent = run.running ? (last.length > 60 ? last.slice(0, 60) + '…' : last || 'Running…') : last.startsWith('error') ? 'Last run failed' : 'Idle';
  $('runpill').title = last;
  $('scrape').disabled = $('scrapeFull').disabled = run.running;
  document.querySelectorAll('.sitebtn').forEach(b => b.disabled = run.running);
}
function openInvite() {
  $('inviteOut').hidden = true; $('inviteMsg').textContent = '';
  $('invite').showModal(); $('inviteWho').focus();
}
async function createInvite() {
  const who = $('inviteWho').value.trim(), b = $('inviteGo');
  if (!who || b.disabled) return;
  b.disabled = true; b.textContent = 'Creating…';
  $('inviteOut').hidden = true; $('inviteMsg').textContent = '';
  try {
    const r = await post('/api/invite', {who}), d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.statusText);
    $('inviteUrl').value = d.url; $('inviteOut').hidden = false;
    $('inviteMsg').textContent = `For ${d.user} (${d.provider === 'AAD' ? 'Microsoft' : 'GitHub'}), valid until ${d.expires}.`;
  } catch (e) {
    $('inviteMsg').textContent = 'No link: ' + e.message;
  } finally {
    b.disabled = false; b.textContent = 'Create link';
  }
}
function copyInvite() {
  $('inviteUrl').select();
  navigator.clipboard.writeText($('inviteUrl').value)
    .then(() => $('inviteMsg').textContent = 'Copied. ' + $('inviteMsg').textContent.replace(/^Copied\. /, ''), () => {});
}
async function downloadPdf() {
  const b = $('pdfBtn'), label = b.textContent;
  b.disabled = true; b.textContent = 'Building PDF (loads all photos and maps)…';
  try {
    flushNote(); await flushSaves();  // the note being typed belongs in the PDF
    const r = await fetch('/api/pdf');
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await r.blob());
    a.download = (r.headers.get('Content-Disposition') || '').match(/filename=(.+)/)?.[1] || 'flats.pdf';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  } catch (e) {
    $('runpill').textContent = 'PDF failed'; $('runpill').title = String(e);
    $('log').textContent = 'PDF export failed: ' + e.message; $('log').hidden = false;
  } finally {
    b.disabled = false; b.textContent = label;
  }
}
function toggleLog() { $('log').hidden = !$('log').hidden; $('log').scrollTop = 1e9; }
// The log every 1.5 s (a tiny request); the listings, which take a while to send and draw, every 10 s and at the end.
let lastListings = 0;
async function poll() {
  let r;
  try { r = (await (await fetch('/api/run')).json()).run; }
  catch (e) { setTimeout(poll, 3000); return; }  // a failed poll must not freeze the log while a run is going
  data.run = r; showRun(r);
  if (!r.running || Date.now() - lastListings > 10000) {
    lastListings = Date.now();
    // the list (not the form, which may hold unsaved edits) so results appear as they come in
    try { await refresh(); } catch { /* next round */ }
  }
  if (r.running) setTimeout(poll, 1500);
}

// ---------- navigation, events, keyboard ----------

function setTab(t) {
  tab = t;
  document.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  $('tab-listings').hidden = t !== 'listings'; $('tab-settings').hidden = t !== 'settings';
  if (t === 'listings' && listStale) renderList();
}
function setView(v) {
  view = v; lsSet('view', v);
  listStale = true; rendered = 0;
  setTab('listings');
  document.querySelector('#detail').scrollTop = 0;
  if (cur()) select(selected);
}

if (!STATIC) document.querySelector('.tabs').onclick = e => e.target.dataset.tab && setTab(e.target.dataset.tab);
$('viewSeg').onclick = e => e.target.dataset.view && setView(e.target.dataset.view);
$('statusFilter').onclick = e => { const b = e.target.closest('[data-s]'); if (b) setFilter('status', b.dataset.s); };
$('cards').onclick = e => {
  const c = e.target.closest('.card');
  if (!c) return;
  if (e.target.closest('[data-act=star]')) return toggleStar(c.dataset.key);
  select(c.dataset.key, false);
};
$('rows').onclick = e => {
  const tr = e.target.closest('tr[data-key]');
  if (tr && !e.target.closest('a, button, input')) select(tr.dataset.key, false);
};
document.querySelectorAll('th[data-k]').forEach(th => th.onclick = () => setSort(th.dataset.k));
document.querySelectorAll('.sitebtn').forEach(b => {
  b.title = `Scrape only ${b.dataset.site}, new listings only (other portals untouched)`;
  b.onclick = e => { e.preventDefault(); saveState().then(() => post('/api/scrape', {quick: true, sites: [b.dataset.site]})).then(poll); };
});
$('runpill').onclick = toggleLog;
// portals may refuse to serve a photo to other sites: blank it instead of showing a broken-image icon
document.addEventListener('error', e => e.target.tagName === 'IMG' && e.target.classList.add('broken'), true);
$('lightbox').onclick = e => { if (e.target.id === 'lightbox' || e.target.id === 'lbImg') closeLightbox(); };

document.addEventListener('keydown', e => {
  const t = e.target, typing = /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName);
  if (e.key === 'Escape') {
    if (!$('lightbox').hidden) closeLightbox(); else if (typing) t.blur();
    return;
  }
  if (typing || e.metaKey || e.ctrlKey || e.altKey || $('help').open || $('invite')?.open || tab !== 'listings' || !data) return;
  const lb = !$('lightbox').hidden;
  switch (e.key) {
    case 'j': case 'ArrowDown': move(1); break;
    case 'k': case 'ArrowUp': move(-1); break;
    case 'ArrowRight': step(1); break;
    case 'ArrowLeft': step(-1); break;
    case 'f': case ' ': lb ? closeLightbox() : openLightbox(); break;
    case 'p': togglePin(); break;
    case 's': toggleStar(selected); break;
    case 'h': hideSelected(); break;
    case 'n': {
      closeLightbox();
      const box = $('noteBox');
      if (box) { box.focus(); box.selectionStart = box.selectionEnd = box.value.length; }
      break;
    }
    case 't': closeLightbox(); $('tagInput')?.focus(); break;
    case 'o': if (cur()) window.open(cur().url, '_blank', 'noreferrer'); break;
    case 'v': setView(view === 'cards' ? 'table' : 'cards'); break;
    case 'm': setView('map'); break;
    case '/': $('q').focus(); break;
    case '?': $('help').showModal(); break;
    default:
      if (!/^[0-5]$/.test(e.key) || !cur()) return;
      setStatus(selected, ST[+e.key][0]);
  }
  e.preventDefault();
});

renderColumns();
load().then(() => { if (STATIC) return; loadModels(); if (data.run.running) poll(); });
