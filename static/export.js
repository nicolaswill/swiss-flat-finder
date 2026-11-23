'use strict';
// PDF export: every listing you marked maybe or shortlist (unless hidden), with an overview map and table that link to
// each listing, then one compact block per listing: key facts, your notes, a zoomed-out locator map, photos. Sets window.exportReady once the
// maps and photos have loaded (or given up), which is what GET /api/pdf waits for before printing.

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
const fmt = n => n == null || n === '' ? '–' : typeof n === 'number' ? n.toLocaleString('de-CH') : esc(n);
const STATUSES = {shortlist: 'Shortlist', maybe: 'Maybe'};
const ST_COLOR = {shortlist: '#16a34a', maybe: '#d97706'};
const LOCATOR_ZOOM = 12;  // town and surroundings
const WAIT_MS = 60000;  // longest wait for tiles and photos before printing anyway
const PHOTO_PX = 360;  // long side; photos print about 30 mm wide, so larger ones are shrunk to this (keeps the PDF small)
const PHOTO_QUALITY = .65;
// printed via /api/pdf, which serves photos with a CORS header: then they may be drawn on a canvas to shrink them
const FOR_PDF = new URLSearchParams(location.search).has('pdf');
window.shrunk = {};

const user = l => l.user || {};
const addressOf = l => [l.address, [l.zip, l.city].filter(Boolean).join(' ')].filter(Boolean).join(', ');
const chfM2 = l => l.rent_chf && l.area_m2 ? Math.round(l.rent_chf / l.area_m2 * 10) / 10 : null;
const statusPill = s => `<span class="st st-${s}">${STATUSES[s]}</span>`;
const img = src => `<img src="${esc(src)}" referrerpolicy="no-referrer" alt=""${FOR_PDF ? ' crossorigin="anonymous"' : ''}>`;
function shrink(im) {  // -> a promise for the (possibly shrunk) photo having loaded
  const scale = PHOTO_PX / Math.max(im.naturalWidth, im.naturalHeight);
  if (!FOR_PDF || scale >= 1) return null;
  const c = document.createElement('canvas');
  c.width = Math.round(im.naturalWidth * scale); c.height = Math.round(im.naturalHeight * scale);
  c.getContext('2d').drawImage(im, 0, 0, c.width, c.height);
  window.shrunk[im.src] = c.toDataURL('image/jpeg', PHOTO_QUALITY);  // /api/pdf caches these instead of the originals
  return new Promise(done => { im.onload = im.onerror = done; im.src = window.shrunk[im.src]; });
}
// Portals send descriptions as plain text or as HTML: keep the formatting tags, drop everything else (scripts, links, styles).
const KEEP_TAGS = new Set(['B', 'STRONG', 'I', 'EM', 'U', 'BR', 'P', 'UL', 'OL', 'LI']);
function descHtml(s) {
  if (!/<[a-z][^>]*>/i.test(s)) return `<div class="desc plain">${esc(s)}</div>`;
  const clean = n => [...n.childNodes].map(c => c.nodeType === 3 ? esc(c.textContent)
    : c.nodeType !== 1 || ['SCRIPT', 'STYLE'].includes(c.tagName) ? ''
    : KEEP_TAGS.has(c.tagName) ? (c.tagName === 'BR' ? '<br>' : `<${c.tagName}>${clean(c)}</${c.tagName}>`)
    : /^(DIV|H\d)$/.test(c.tagName) ? `<p>${clean(c)}</p>` : clean(c)).join('');
  return `<div class="desc">${clean(new DOMParser().parseFromString(s, 'text/html').body)}</div>`;
}
function orderedImages(l) {  // pinned photos first, as in the app
  const imgs = l.images || [], pins = (user(l).pinned || []).filter(p => imgs.includes(p));
  return [...pins, ...imgs.filter(i => !pins.includes(i))];
}
// shortlist before maybe, starred first, then cheapest (tax-adjusted when taxes are on)
const rentOf = l => l.rent_tax_adjusted_chf ?? l.rent_chf ?? Infinity;
const order = ([, a], [, b]) => Object.keys(STATUSES).indexOf(user(a).status) - Object.keys(STATUSES).indexOf(user(b).status)
  || !!user(b).starred - !!user(a).starred || rentOf(a) - rentOf(b);

function commuteLines(l, points) {
  return (l.commute || []).map((r, i) => {
    const p = points[i];
    if (!p || r == null) return '';
    if (r === 'no route') return `${esc(p.name)}: no route`;
    const rides = (r.rides || []).map(x => x.line).join(' → ');
    return `${esc(p.name)}: ${r.min} min` + (p.mode === 'transit' ? `, ${r.transfers} transfer(s)${rides ? ', ' + esc(rides) : ''}` : `, ${r.km} km`)
      + (r.night_min != null ? `, night return ${r.night_min} min` : '');
  }).filter(Boolean);
}
function aiBadges(a) {
  if (!a) return '';
  const flags = ['temporary', 'furnished', 'swap', 'shared'].filter(k => a[k] && (a.evidence || {})[k])
    .map(k => k === 'temporary' && a.temporary_until ? `temporary → ${a.temporary_until}` : {swap: 'Tauschwohnung', shared: 'WG'}[k] || k);
  const rules = (a.exclusions || []).filter(x => x.matches && x.evidence).map(x => x.criterion);
  return [...flags, ...rules].map(f => `<span class="badge">${esc(f)}</span>`).join('');
}

const numBadge = (n, l) => `<a href="#l${n}" class="num" style="background: ${ST_COLOR[user(l).status]}">${n}</a>`;
function numIcon(n, l) {  // a link: clicking a pin in the PDF jumps to that listing
  return L.divIcon({className: '', iconSize: [16, 16], iconAnchor: [8, 8], html: numBadge(n, l)});
}
function newMap(el) {
  const map = L.map(el, {zoomControl: false, fadeAnimation: false, zoomAnimation: false, markerZoomAnimation: false,
    inertia: false, dragging: false, scrollWheelZoom: false});
  const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'}).addTo(map);
  return {map, loaded: new Promise(done => tiles.once('load', done))};
}

function render(data) {
  const points = data.state.commute.points, taxOn = data.state.tax.income > 0;
  const rs = Object.entries(data.listings)
    .filter(([, l]) => STATUSES[user(l).status] && l.excluded !== 'hidden' && !l.dup_of).sort(order);
  const date = new Date().toLocaleDateString('de-CH');
  if (!rs.length) {
    $('out').innerHTML = `<h1>Flats</h1><div class="empty">No listings marked Maybe or Shortlist yet.</div>`;
    return [];
  }
  const counts = Object.keys(STATUSES).map(s => `${rs.filter(([, l]) => user(l).status === s).length} ${STATUSES[s].toLowerCase()}`).join(', ');
  const nolocation = rs.filter(([, l]) => l.lat == null || l.lon == null).length;
  $('out').innerHTML = `
  <h1 id="top">Flats: shortlist and maybe</h1>
  <p class="muted">${rs.length} listings (${counts}), exported ${esc(date)}${nolocation ? `, ${nolocation} without a location on the map` : ''}</p>
  <div id="overview" class="map"></div>
  <table class="summary"><thead><tr><th>#</th><th>Status</th><th>Listing</th><th class="r">CHF</th>${taxOn ? '<th class="r">Tax-adj.</th>' : ''}
    <th class="r">Rooms</th><th class="r">m²</th><th>Floor</th><th>Available</th></tr></thead><tbody>
    ${rs.map(([, l], i) => `<tr><td>${numBadge(i + 1, l)}</td><td>${statusPill(user(l).status)}${user(l).starred ? ' ★' : ''}</td>
      <td><a href="#l${i + 1}">${esc(l.title || l.id)}</a><br><span class="muted">${esc(addressOf(l))}</span></td>
      <td class="r">${fmt(l.rent_chf)}</td>${taxOn ? `<td class="r">${fmt(l.rent_tax_adjusted_chf)}</td>` : ''}
      <td class="r">${fmt(l.rooms)}</td><td class="r">${fmt(l.area_m2)}</td><td>${esc(l.floor || '–')}</td><td>${esc(l.available_from || '–')}</td></tr>`).join('')}
  </tbody></table>
  ${rs.map(([k, l], i) => listing(k, l, i + 1, points, taxOn)).join('')}`;

  const waits = [], pts = [];
  if (window.L) {
    const o = newMap('overview');
    rs.forEach(([, l], i) => {
      if (l.lat == null || l.lon == null) return;
      L.marker([l.lat, l.lon], {icon: numIcon(i + 1, l)}).addTo(o.map);
      pts.push([l.lat, l.lon]);
    });
    if (pts.length) { o.map.fitBounds(pts, {padding: [20, 20], maxZoom: 14}); waits.push(o.loaded); }
    rs.forEach(([, l], i) => {
      if (l.lat == null || l.lon == null) return;
      const m = newMap(`map${i + 1}`);
      m.map.setView([l.lat, l.lon], LOCATOR_ZOOM);
      L.marker([l.lat, l.lon], {icon: numIcon(i + 1, l)}).addTo(m.map);
      waits.push(m.loaded);
    });
  } else {
    document.querySelectorAll('.map').forEach(e => e.innerHTML = '<div class="empty">The map library could not be loaded (offline?).</div>');
  }
  for (const im of document.querySelectorAll('.photos img'))
    waits.push(im.complete ? shrink(im) : new Promise(done => { im.onload = () => done(shrink(im)); im.onerror = done; }));
  return waits;
}

function listing(k, l, n, points, taxOn) {
  const u = user(l), photos = orderedImages(l), ai = aiBadges(l.ai);
  const fact = (v, label) => v == null || v === '' ? '' : `<div><b>${v}</b> <span>${label}</span></div>`;
  const line = (label, html) => html ? `<p><span class="lbl">${label}</span>${html}</p>` : '';
  const conflicts = Object.entries(l.conflicts || {}).map(([f, c]) => `${esc(f)}: ${esc(c)}`).join('; ');
  const hasMap = l.lat != null && l.lon != null;
  return `<section class="listing" id="l${n}">
    <div class="top">
      <div class="head">${numBadge(n, l)}<h2><a href="${esc(l.url)}">${esc(l.title || l.id)}</a></h2>
        <span class="price">CHF ${fmt(l.rent_chf)}</span></div>
      <div class="meta">${statusPill(u.status)}${u.starred ? ' ★' : ''} ${esc(addressOf(l))} · ${esc(l.site)}${l.provider || l.provider_label ? ' · ' + esc(l.provider || l.provider_label) : ''}
        · <a href="${esc(l.url)}">open listing</a> · <a href="#top">↑ overview</a></div>
      <div class="strip">
        ${fact(fmt(l.rooms), 'rooms')}${fact(fmt(l.area_m2), 'm²')}${fact(esc(l.floor), 'floor')}${fact(fmt(chfM2(l)), 'CHF/m²')}
        ${taxOn ? fact(fmt(l.rent_tax_adjusted_chf), `tax-adj. (${l.tax_extra_month_chf != null ? '+' + fmt(l.tax_extra_month_chf) : '?'})`) : ''}
        ${fact(esc(l.available_from || '–'), 'available' + (l.available_source === 'AI' ? ' (AI)' : ''))}
        ${l.ai ? fact(esc(l.ai.kitchen), 'kitchen') : ''}
        ${fact(esc((l.published || l.first_seen || '').slice(0, 10)), l.published ? 'published' : 'first seen')}
      </div>
      <div class="cols"><div>
        ${u.note ? `<div class="note">${esc(u.note)}</div>` : ''}
        ${line('Tags', (u.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join(''))}
        ${line('Commute', commuteLines(l, points).join('<br>'))}
        ${line('AI', [ai, l.ai && l.ai.summary ? esc(l.ai.summary) : ''].filter(Boolean).join(' '))}
        ${line('⚠', conflicts)}
        ${line('Excluded', l.excluded ? esc(l.excluded) + (l.deleted_at ? ` (removed ${esc(l.deleted_at.slice(0, 10))})` : '') : '')}
      </div>${hasMap ? `<div id="map${n}" class="map"></div>` : '<div></div>'}</div>
    </div>
    ${photos.length ? `<div class="photos">${photos.map(img).join('')}</div>` : ''}
    ${l.description ? descHtml(l.description) : ''}
  </section>`;
}

document.addEventListener('error', e => e.target.tagName === 'IMG' && e.target.classList.add('broken'), true);
fetch('/api/data').then(r => r.json()).then(data => {
  const waits = render(data);
  const timeout = new Promise(done => setTimeout(done, WAIT_MS));
  return Promise.race([Promise.all(waits), timeout]);
}).catch(e => {
  $('out').innerHTML = `<div class="empty">Could not load the listings: ${esc(e)}</div>`;
}).finally(() => { window.exportReady = true; });
