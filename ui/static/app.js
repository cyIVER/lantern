/* LANtern CS2 control UI.
   Polling rather than websockets: the backend already proxies two upstreams
   (Pelican + RCON) and a 4s poll is imperceptible on a LAN, while a socket
   would add reconnect/auth-refresh state for no real gain. */

const $  = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let CONFIG = null;      // /api/config
let CURRENT_MAP = null; // from RCON status
let BUSY = false;

const MAP_KIND = (m) =>
  m.startsWith('de_') ? 'Defusal' :
  m.startsWith('cs_') ? 'Hostage' :
  m.startsWith('ar_') ? 'Arms Race' : 'Other';

function toast(msg, bad = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.toggle('bad', bad);
  t.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), 3800);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    method: opts.method || (opts.body ? 'POST' : 'GET'),
  });
  const text = await r.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!r.ok) throw new Error(data.detail || data.raw || `HTTP ${r.status}`);
  return data;
}

/* ------------------------------------------------------------------- tabs */
$$('.tab').forEach((b) => b.addEventListener('click', () => {
  $$('.tab').forEach((x) => x.classList.remove('active'));
  $$('.panel').forEach((x) => x.classList.remove('active'));
  b.classList.add('active');
  $('#tab-' + b.dataset.tab).classList.add('active');
}));

/* ------------------------------------------------------------------ power */
$$('[data-power]').forEach((b) => b.addEventListener('click', async () => {
  const signal = b.dataset.power;
  if (signal === 'kill' && !confirm('Force kill the server? Unsaved state is lost.')) return;
  try {
    await api('/api/power', { body: { signal } });
    toast(`Sent ${signal}.`);
  } catch (e) { toast(e.message, true); }
}));

/* ------------------------------------------------------------------ vitals */
function fmtUptime(s) {
  if (!s) return '—';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

async function pollState() {
  try {
    const s = await api('/api/state');
    const pill = $('#v-state');
    pill.textContent = s.state;
    pill.className = 'pill ' + (s.state || 'unknown');
    $('#v-cpu').textContent = s.cpu + '%';
    $('#v-ram').textContent = s.memory_mb + ' MB';
    $('#v-uptime').textContent = fmtUptime(s.uptime_s);
  } catch {
    $('#v-state').textContent = 'unreachable';
    $('#v-state').className = 'pill offline';
  }
}

/* ----------------------------------------------------------------- players */
function playerRow(p) {
  const tr = document.createElement('tr');
  const sid = p.steamid64 || '';
  // Players without a validated SteamID (sv_lan skips Steam auth) can still be
  // kicked/slayed/swapped by slot, but not banned or muted -- those need to
  // outlive the session.
  const acts = p.bot
    ? `<button class="btn tiny stop" data-act="kick">Kick Bot</button>`
    : `<button class="btn tiny" data-act="swap">Swap</button>
       <button class="btn tiny warn" data-act="slay">Slay</button>
       <button class="btn tiny warn" data-act="mute" ${p.identified ? '' : 'disabled title="needs a SteamID"'}>Mute</button>
       <button class="btn tiny stop" data-act="kick">Kick</button>
       <button class="btn tiny kill" data-act="ban" ${p.identified ? '' : 'disabled title="needs a SteamID"'}>Ban</button>`;
  const idCell = p.bot
    ? '<span style="color:#5d6b7c">—</span>'
    : (sid || '<span style="color:#d99b2b" title="sv_lan skips Steam auth">no SteamID</span>');
  tr.innerHTML = `
    <td>${p.bot ? '<span class="tag bot">BOT</span>' : '<span class="tag human">P</span>'}
        <span class="sub" style="color:#5d6b7c">#${p.slot}</span></td>
    <td>${escapeHtml(p.name)}</td>
    <td class="sid">${idCell}</td>
    <td class="right"><span class="rowacts">${acts}</span></td>`;
  tr.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', async () => {
    const action = b.dataset.act;
    let duration = 0;
    if (action === 'ban' || action === 'mute') {
      const ans = prompt(`${action} ${p.name} for how many minutes? (0 = permanent)`, '30');
      if (ans === null) return;
      duration = parseInt(ans, 10) || 0;
    } else if (action === 'kick' && !confirm(`Kick ${p.name}?`)) return;
    try {
      const r = await api('/api/player', {
        body: { steamid64: sid || null, slot: p.slot, name: p.name,
                bot: !!p.bot, action, duration },
      });
      toast(`${action} → ${p.name}`);
      if (r.output) appendOutput(r.output);
      setTimeout(pollPlayers, 700);
    } catch (e) { toast(e.message, true); }
  }));
  return tr;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function pollPlayers() {
  try {
    const d = await api('/api/players');
    if (d.map) {
      CURRENT_MAP = d.map;
      $('#v-map').textContent = CURRENT_MAP;
      markCurrentMap();
    }
    const list = d.players || [];
    $('#v-players').textContent = `${d.humans ?? 0} + ${d.bots ?? 0} bots`;
    const tbody = $('#players-table tbody');
    tbody.replaceChildren(...list.map(playerRow));
    $('#players-table').hidden = list.length === 0;
    $('#players-empty').hidden = list.length > 0;
    $('#players-hint').textContent = d.error
      ? d.error
      : (d.hibernating ? 'server hibernating' : 'polling every 4s');
  } catch (e) {
    $('#players-hint').textContent = e.message;
  }
}

/* -------------------------------------------------------------------- maps */
function markCurrentMap() {
  $$('.mapcard').forEach((c) => c.classList.toggle('current', c.dataset.map === CURRENT_MAP));
}

function buildMaps() {
  const grid = $('#map-grid');
  grid.replaceChildren(...CONFIG.maps.map((m) => {
    const el = document.createElement('div');
    el.className = 'mapcard';
    el.dataset.map = m;
    const hasIcon = CONFIG.have_icon.includes(m);
    el.innerHTML = `
      ${hasIcon ? `<img src="/static/maps/${m}.svg" alt="" loading="lazy">` : '<div style="height:76px"></div>'}
      <div class="nm">${m}</div>
      <div class="badge">${MAP_KIND(m)}</div>`;
    el.addEventListener('click', async () => {
      if (BUSY) return;
      BUSY = true;
      try {
        const r = await api('/api/map', { body: { map: m, persist: $('#map-persist').checked } });
        toast(r.switched ? `Changing level to ${m}…` : (r.note || `Boot map set to ${m}`),
              !r.switched);
        setTimeout(pollPlayers, 2500);
      } catch (e) { toast(e.message, true); }
      finally { BUSY = false; }
    });
    return el;
  }));
  markCurrentMap();
}

/* -------------------------------------------------------------------- mode */
function buildModes() {
  const cur = CONFIG.variables.MODE?.value;
  $('#mode-grid').replaceChildren(...CONFIG.modes.map((m) => {
    const el = document.createElement('div');
    el.className = 'modecard' + (m.id === cur ? ' current' : '');
    el.innerHTML = `<h3>${m.id}</h3><p>${m.blurb}</p>`;
    el.addEventListener('click', async () => {
      if (m.id === cur) return toast('Already in that mode.');
      if (!confirm(`Switch to ${m.id}? The server restarts (~40s).`)) return;
      try {
        await api('/api/mode', { body: { mode: m.id } });
        toast(`Switching to ${m.id} — restarting…`);
        setTimeout(loadConfig, 45000);
      } catch (e) { toast(e.message, true); }
    });
    return el;
  }));
}

function bindSlider(inputSel, outSel, envKey, live) {
  const input = $(inputSel), out = $(outSel);
  const v = CONFIG.variables[envKey]?.value;
  if (v !== undefined) { input.value = v; out.textContent = v; }
  input.oninput = () => { out.textContent = input.value; };
  input.onchange = async () => {
    try {
      await api('/api/variable', { method: 'PUT', body: { key: envKey, value: input.value } });
      if (live) await api('/api/command', { body: { command: `${live} ${input.value}` } });
      toast(`${envKey} = ${input.value}`);
    } catch (e) { toast(e.message, true); }
  };
}

function buildToggles() {
  const defs = [
    ['ENABLE_SKINS', 'Weapon skins'],
    ['VAC_ENABLED', 'VAC secure'],
    ['AUTO_UPDATE', 'Auto-update on boot'],
    ['RCON_ENABLED', 'RCON enabled'],
  ];
  $('#toggles').replaceChildren(...defs.map(([key, label]) => {
    const wrap = document.createElement('label');
    wrap.className = 'switch';
    const on = String(CONFIG.variables[key]?.value) === '1';
    wrap.innerHTML = `<input type="checkbox" ${on ? 'checked' : ''}><span>${label}</span>`;
    wrap.querySelector('input').addEventListener('change', async (e) => {
      try {
        await api('/api/variable', {
          method: 'PUT', body: { key, value: e.target.checked ? '1' : '0' },
        });
        toast(`${label} ${e.target.checked ? 'on' : 'off'} — restart to apply`);
      } catch (err) { toast(err.message, true); }
    });
    return wrap;
  }));
}

/* ------------------------------------------------------------------- match */
$$('[data-match]').forEach((b) => b.addEventListener('click', async () => {
  try {
    const r = await api('/api/match/' + b.dataset.match, { method: 'POST' });
    toast(`${b.textContent} sent`);
    if (r.output) appendOutput(r.output);
  } catch (e) { toast(e.message, true); }
}));

/* ----------------------------------------------------------------- console */
function appendOutput(text) {
  const o = $('#output');
  o.textContent += (o.textContent.endsWith('\n') ? '' : '\n') + text.trimEnd() + '\n';
  o.scrollTop = o.scrollHeight;
}

$('#cmd-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('#cmd');
  const cmd = input.value.trim();
  if (!cmd) return;
  appendOutput(`> ${cmd}`);
  input.value = '';
  try {
    const r = await api('/api/command', { body: { command: cmd } });
    appendOutput(r.output || r.note || '(no output)');
  } catch (err) { appendOutput('ERROR: ' + err.message); }
});

/* -------------------------------------------------------------------- boot */
async function loadConfig() {
  CONFIG = await api('/api/config');
  buildMaps();
  buildModes();
  buildToggles();
  bindSlider('#bot-quota', '#out-quota', 'BOT_QUOTA', 'bot_quota');
  bindSlider('#bot-diff', '#out-diff', 'BOT_DIFFICULTY', 'bot_difficulty');
}

(async function init() {
  try { await loadConfig(); }
  catch (e) { toast('Config load failed: ' + e.message, true); }
  pollState(); pollPlayers();
  setInterval(pollState, 4000);
  setInterval(pollPlayers, 4000);
})();

/* ------------------------------------------------------------------ loadout
   Writes go straight into WeaponPaints' tables. The catalogue comes from the
   plugin's own bundled JSON, so it always matches the installed version.
   Item images are hosted on GitHub; they degrade to a placeholder offline. */

let LO = { knives: null, gloves: null, weapons: null, current: null };

function loSteamId() {
  const typed = $('#lo-steamid').value.trim();
  if (typed) return typed;
  return $('#lo-player').value || '';
}

function imgTag(src, alt) {
  if (!src) return '<div style="height:72px;background:#0b0e12;margin-bottom:6px"></div>';
  return `<img src="${src}" alt="${escapeHtml(alt)}" loading="lazy"
           onerror="this.style.visibility='hidden'">`;
}

async function loRefreshPlayers() {
  try {
    const d = await api('/api/players');
    // A loadout is keyed on SteamID, so unauthenticated players cannot have one.
    const humans = (d.players || []).filter((p) => !p.bot && p.steamid64);
    const sel = $('#lo-player');
    const prev = sel.value;
    sel.replaceChildren(...[
      new Option('— pick a connected player —', ''),
      ...humans.map((p) => new Option(`${p.name} (${p.steamid64})`, p.steamid64)),
    ]);
    if (prev) sel.value = prev;
  } catch { /* roster unavailable; the manual SteamID box still works */ }
}

async function loLoadCurrent() {
  const sid = loSteamId();
  const el = $('#lo-current');
  if (!sid) { el.textContent = 'Pick a player, or paste a SteamID64.'; LO.current = null; return; }
  try {
    LO.current = await api(`/api/loadout/${sid}`);
    const n = Object.keys(LO.current.skins || {}).length;
    el.textContent = `Knife: ${LO.current.knife || 'default'} · Gloves: ${LO.current.gloves || 'default'} · ${n} weapon skin(s) set`;
  } catch (e) { el.textContent = e.message; LO.current = null; }
  markLoCurrent();
}

function markLoCurrent() {
  const cur = LO.current || {};
  $$('#grid-knife .item').forEach((c) =>
    c.classList.toggle('current', c.dataset.weapon === cur.knife));
  $$('#grid-gloves .item').forEach((c) =>
    c.classList.toggle('current', Number(c.dataset.defindex) === Number(cur.gloves)));
}

async function loBuildKnives() {
  LO.knives = LO.knives || await api('/api/loadout/catalog/knives');
  $('#grid-knife').replaceChildren(...LO.knives.map((k) => {
    const el = document.createElement('div');
    el.className = 'item';
    el.dataset.weapon = k.weapon_name;
    el.innerHTML = `${imgTag(k.image, k.label)}
      <div class="nm">${escapeHtml(k.label)}</div>
      <div class="sub">pick a finish →</div>`;
    // Two steps: choose the model, then its finish. A knife needs both the
    // model row and a paint row, so applying the model alone gives vanilla.
    el.onclick = () => loBuildKnifeFinishes(k);
    return el;
  }));
  markLoCurrent();
}

async function loBuildKnifeFinishes(knife) {
  const grid = $('#grid-knife');
  const paints = await api(`/api/loadout/catalog/skins/${knife.weapon_name}`);

  const back = document.createElement('div');
  back.className = 'item';
  back.style.borderColor = 'var(--gold)';
  back.innerHTML = `<div style="height:72px;display:flex;align-items:center;
                     justify-content:center;font-size:26px;color:var(--gold)">←</div>
                    <div class="nm">All knives</div>
                    <div class="sub">${escapeHtml(knife.label)}</div>`;
  back.onclick = loBuildKnives;

  grid.replaceChildren(back, ...paints.map((pt) => {
    const el = document.createElement('div');
    el.className = 'item';
    // Doppler and similar share a name across several paint ids (the phases),
    // so show the id to tell them apart.
    const short = (pt.paint_name || '').split('|').slice(1).join('|').trim()
                  || pt.paint_name || 'Default';
    el.innerHTML = `${imgTag(pt.image, pt.paint_name)}
      <div class="nm">${escapeHtml(short)}</div>
      <div class="sub">paint ${pt.paint}</div>`;
    el.onclick = () => loApply('/api/loadout/knife', {
      steamid64: loSteamId(),
      weapon_name: knife.weapon_name,
      weapon_defindex: pt.weapon_defindex ?? knife.weapon_defindex,
      paint: pt.paint,
    }, `${knife.label} | ${short}`);
    return el;
  }));
}

async function loBuildGloves() {
  LO.gloves = LO.gloves || await api('/api/loadout/catalog/gloves');
  $('#grid-gloves').replaceChildren(...LO.gloves.map((g) => {
    const el = document.createElement('div');
    el.className = 'item';
    el.dataset.defindex = g.weapon_defindex;
    el.innerHTML = `${imgTag(g.image, g.paint_name)}<div class="nm">${escapeHtml(g.paint_name)}</div>`;
    el.onclick = () => loApply('/api/loadout/gloves',
      { steamid64: loSteamId(), weapon_defindex: g.weapon_defindex, paint: g.paint },
      g.paint_name);
    return el;
  }));
  markLoCurrent();
}

async function loBuildWeapons() {
  // Arsenal view: every weapon, grouped, each showing the skin currently
  // assigned. Assigning a skin never gives you the gun -- WeaponPaints only
  // paints what you buy or pick up -- so a full loadout can be prepared before
  // a match without affecting competitive play.
  LO.weapons = LO.weapons || await api('/api/loadout/catalog/weapons');
  $('#lo-weapon').closest('.picker').style.display = 'none';

  const assigned = (LO.current && LO.current.skins) || {};
  const grid = $('#grid-skins');
  const groups = {};
  LO.weapons.forEach((w) => (groups[w.category] ||= []).push(w));

  const nodes = [];
  for (const [cat, list] of Object.entries(groups)) {
    const head = document.createElement('div');
    head.className = 'cathead';
    head.textContent = cat;
    nodes.push(head);
    list.forEach((w) => {
      const paint = assigned[String(w.weapon_defindex)];
      const el = document.createElement('div');
      el.className = 'item' + (paint ? ' current' : '');
      el.innerHTML = `
        <div class="nm">${escapeHtml(w.label)}</div>
        <div class="sub">${paint ? 'paint ' + paint : 'default'}</div>`;
      el.onclick = () => loBuildSkinsFor(w);
      nodes.push(el);
    });
  }
  grid.replaceChildren(...nodes);
}

async function loBuildSkinsFor(weapon) {
  const skins = await api(`/api/loadout/catalog/skins/${weapon.weapon_name}`);

  const back = document.createElement('div');
  back.className = 'item';
  back.style.borderColor = 'var(--gold)';
  back.innerHTML = `<div style="height:72px;display:flex;align-items:center;
                     justify-content:center;font-size:26px;color:var(--gold)">←</div>
                    <div class="nm">All weapons</div>
                    <div class="sub">${escapeHtml(weapon.label)}</div>`;
  back.onclick = loBuildWeapons;

  $('#grid-skins').replaceChildren(back, ...skins.map((sk) => {
    const el = document.createElement('div');
    el.className = 'item';
    const short = (sk.paint_name || '').split('|').slice(1).join('|').trim()
                  || sk.paint_name || 'Default';
    el.innerHTML = `${imgTag(sk.image, sk.paint_name)}
      <div class="nm">${escapeHtml(short)}</div>
      <div class="sub">paint ${sk.paint}</div>`;
    el.onclick = () => loApply('/api/loadout/skin', {
      steamid64: loSteamId(),
      weapon_defindex: sk.weapon_defindex ?? weapon.weapon_defindex,
      paint: sk.paint,
    }, `${weapon.label} | ${short}`);
    return el;
  }));
}

async function loApply(path, body, label) {
  if (!body.steamid64) return toast('Pick a player first.', true);
  try {
    const r = await api(path, { body });
    toast(`${label} set — ${r.note || 'done'}`);
    loLoadCurrent();
  } catch (e) { toast(e.message, true); }
}

$$('.subtab').forEach((b) => b.addEventListener('click', async () => {
  $$('.subtab').forEach((x) => x.classList.remove('active'));
  $$('.lopanel').forEach((x) => x.classList.remove('active'));
  b.classList.add('active');
  $('#lo-' + b.dataset.lo).classList.add('active');
  if (b.dataset.lo === 'knife')  await loBuildKnives();
  if (b.dataset.lo === 'gloves') await loBuildGloves();
  if (b.dataset.lo === 'skins')  { await loLoadCurrent(); await loBuildWeapons(); }
}));

$('#lo-player').addEventListener('change', () => { $('#lo-steamid').value = ''; loLoadCurrent(); });
$('#lo-steamid').addEventListener('change', loLoadCurrent);

$('#lo-clear').addEventListener('click', async () => {
  const sid = loSteamId();
  if (!sid) return toast('Pick a player first.', true);
  if (!confirm(`Clear the entire loadout for ${sid}?`)) return;
  try {
    await api(`/api/loadout/${sid}`, { method: 'DELETE' });
    toast('Loadout cleared.');
    loLoadCurrent();
  } catch (e) { toast(e.message, true); }
});

// Populate the tab the first time it is opened, and keep the roster fresh.
document.querySelector('[data-tab="loadout"]').addEventListener('click', async () => {
  await loRefreshPlayers();
  await loBuildKnives();
  loLoadCurrent();
});
setInterval(() => {
  if (document.querySelector('[data-tab="loadout"]').classList.contains('active')) {
    loRefreshPlayers();
  }
}, 8000);

/* ------------------------------------------------------------------ presets
   Slots 1-9. "Save" snapshots whatever the player currently has; the console
   watcher applies them when someone types !1 .. !9 in chat. */

async function loBuildPresets() {
  const wrap = $('#preset-list');
  const sid = loSteamId();
  if (!sid) {
    wrap.replaceChildren(Object.assign(document.createElement('div'), {
      className: 'empty', textContent: 'Pick a player to manage their presets.',
    }));
    return;
  }
  let saved = [];
  try { saved = await api(`/api/presets/${sid}`); }
  catch (e) { return toast(e.message, true); }
  const bySlot = Object.fromEntries(saved.map((p) => [p.slot, p]));

  wrap.replaceChildren(...[1, 2, 3, 4, 5, 6, 7, 8, 9].map((slot) => {
    const p = bySlot[slot];
    const el = document.createElement('div');
    el.className = 'preset' + (p ? ' filled' : '');
    el.innerHTML = `
      <div class="slot">!${slot}</div>
      <div class="nm">${p ? escapeHtml(p.name) : '<span style="color:#5d6b7c">empty</span>'}</div>
      <div class="meta">${p ? `${p.knife ? p.knife.replace('weapon_knife_', '').replace('weapon_', '') : 'no knife'} · ${p.count} skin(s)` : ''}</div>
      <div class="row">
        <button class="btn tiny go" data-a="save">Save</button>
        ${p ? '<button class="btn tiny" data-a="apply">Apply</button>' : ''}
        ${p ? '<button class="btn tiny stop" data-a="del">✕</button>' : ''}
      </div>`;

    el.querySelector('[data-a="save"]').onclick = async () => {
      const name = prompt(`Name for preset ${slot}?`, p ? p.name : `Preset ${slot}`);
      if (name === null) return;
      try {
        await api('/api/presets', { body: { steamid64: sid, slot, name } });
        toast(`Saved current loadout to !${slot}`);
        loBuildPresets();
      } catch (e) { toast(e.message, true); }
    };
    const applyBtn = el.querySelector('[data-a="apply"]');
    if (applyBtn) applyBtn.onclick = async () => {
      try {
        const r = await api('/api/presets/apply', { body: { steamid64: sid, slot } });
        toast(r.ok ? `Applied "${r.name}" — !wp or respawn` : r.error, !r.ok);
        loLoadCurrent();
      } catch (e) { toast(e.message, true); }
    };
    const delBtn = el.querySelector('[data-a="del"]');
    if (delBtn) delBtn.onclick = async () => {
      if (!confirm(`Delete preset ${slot}?`)) return;
      try {
        await api(`/api/presets/${sid}/${slot}`, { method: 'DELETE' });
        toast(`Deleted !${slot}`);
        loBuildPresets();
      } catch (e) { toast(e.message, true); }
    };
    return el;
  }));
}

// Presets follow whichever player is selected.
['#lo-player', '#lo-steamid'].forEach((sel) =>
  $(sel).addEventListener('change', loBuildPresets));
document.querySelector('[data-tab="loadout"]').addEventListener('click', loBuildPresets);

/* ---------------------------------------------------------------- stardew */
/* Unlike CS2, this talks to a real REST API rather than parsing console text,
   so the whole tab is a thin render of /api/stardew. That endpoint never
   throws: a farm still loading answers /health long before /status, and a
   partial dashboard is more useful than one error message. */

const SDV_TIMES = [
  [600, '6:00 am — sunrise'], [900, '9:00 am'], [1200, '12:00 pm — noon'],
  [1500, '3:00 pm'], [1800, '6:00 pm'], [2000, '8:00 pm — dusk'],
  [2200, '10:00 pm'], [2400, '12:00 am — midnight'], [2600, '2:00 am — collapse'],
];

let sdvTimer = null;

function sdvStat(dl, label, value) {
  const d = document.createElement('div');
  const dt = document.createElement('dt');
  const dd = document.createElement('dd');
  dt.textContent = label;
  dd.innerHTML = value;
  d.append(dt, dd);
  dl.appendChild(d);
}

async function sdvRefresh() {
  let d;
  try {
    d = await api('/api/stardew');
  } catch (e) {
    $('#sdv-offline').textContent = e.message;
    $('#sdv-offline').hidden = false;
    $('#sdv-body').hidden = true;
    return;
  }

  if (!d.configured) {
    $('#sdv-offline').textContent =
      'Stardew is not wired up. Set STARDEW_API_URL in ui/.env — see docs/STARDEW.md.';
    $('#sdv-offline').hidden = false;
    $('#sdv-body').hidden = true;
    return;
  }

  if (!d.online) {
    const why = d.errors?.health || 'the farm is not running';
    $('#sdv-offline').innerHTML =
      `<span class="sdv-dot sdv-off"></span>Farm is offline — ${why}` +
      `<br><span class="hint">start it with <code>./lantern use stardew</code></span>`;
    $('#sdv-offline').hidden = false;
    $('#sdv-body').hidden = true;
    return;
  }

  $('#sdv-offline').hidden = true;
  $('#sdv-body').hidden = false;

  const st = d.status || {};
  const h = d.health || {};
  const set = d.settings || {};

  $('#sdv-code').textContent = st.steamInviteCode || '—';

  const dl = $('#sdv-stats');
  dl.replaceChildren();
  sdvStat(dl, 'Players', `${st.playerCount ?? 0} / ${st.maxPlayers ?? '?'}`);
  sdvStat(dl, 'Farm', (set.game?.farmName) || '—');
  sdvStat(dl, 'Engine', `<span class="sdv-dot ${h.isFrozen ? 'sdv-off' : 'sdv-on'}"></span>` +
                        (h.isFrozen ? 'frozen' : `${h.lastTickMs ?? '?'} ms/tick`));
  sdvStat(dl, 'Ticks', (h.tickCount ?? 0).toLocaleString());
  sdvStat(dl, 'Render', (d.rendering?.fps ?? 0) > 0 ? `${d.rendering.fps} fps` : 'off');
  sdvStat(dl, 'Version', st.serverVersion || '—');

  // players
  const ps = (d.players?.players) || [];
  const tbl = $('#sdv-players');
  const tb = tbl.querySelector('tbody');
  tb.replaceChildren();
  if (!ps.length) {
    tbl.hidden = true;
    $('#sdv-players-empty').hidden = false;
  } else {
    tbl.hidden = false;
    $('#sdv-players-empty').hidden = true;
    ps.forEach((pl) => {
      const name = pl.name || pl.playerName || pl.farmerName || '(unnamed)';
      const tr = document.createElement('tr');
      const td = (t) => { const c = document.createElement('td'); c.textContent = t; return c; };
      tr.append(td(name), td(pl.isHost ? 'host' : (pl.farmhandName || '—')));

      const act = document.createElement('td');
      act.className = 'right';
      const admin = document.createElement('button');
      admin.className = 'btn';
      admin.textContent = 'Make admin';
      admin.onclick = async () => {
        try { await api('/api/stardew/admin', { body: { name } }); toast(`${name} is now an admin.`); }
        catch (e) { toast(e.message, true); }
      };
      act.appendChild(admin);
      tr.appendChild(act);
      tb.appendChild(tr);
    });
  }

  // cabins
  const cb = d.cabins;
  $('#sdv-cabins').textContent = cb
    ? `${cb.strategy} — ${cb.assignedCount}/${cb.totalCount} assigned, ${cb.availableCount} free`
    : '—';

  $('#sdv-fps').value = String(d.rendering?.fps ?? 0);
}

function sdvInit() {
  const sel = $('#sdv-time');
  if (!sel.options.length) {
    SDV_TIMES.forEach(([v, label]) => sel.add(new Option(label, v)));
    sel.value = '900';
  }

  $('#sdv-copy').onclick = async () => {
    const code = $('#sdv-code').textContent.trim();
    if (!code || code === '—') return;
    try { await navigator.clipboard.writeText(code); toast('Invite code copied.'); }
    catch { toast('Could not copy — select it manually.', true); }
  };

  $('#sdv-set-time').onclick = async () => {
    try {
      await api('/api/stardew/time', { body: { time: Number($('#sdv-time').value) } });
      toast('Time set.');
      sdvRefresh();
    } catch (e) { toast(e.message, true); }
  };

  $('#sdv-set-fps').onclick = async () => {
    const fps = Number($('#sdv-fps').value);
    try {
      await api('/api/stardew/rendering', { body: { fps } });
      toast(fps ? `Rendering at ${fps} fps.` : 'Rendering disabled.');
      sdvRefresh();
    } catch (e) { toast(e.message, true); }
  };

  $('#sdv-reload').onclick = async () => {
    if (!confirm('Reload the world from server-settings.json? Players are briefly disconnected.')) return;
    try { await api('/api/stardew/reload', { body: {} }); toast('World reloaded.'); }
    catch (e) { toast(e.message, true); }
  };

  $('#sdv-shot-btn').onclick = async () => {
    const img = $('#sdv-shot-img');
    try {
      const r = await fetch('/api/stardew/screenshot');
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      const blob = await r.blob();
      if (img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);
      img.src = URL.createObjectURL(blob);
      img.hidden = false;
    } catch (e) {
      toast(`${e.message} — set the render rate above 0 first.`, true);
    }
  };
}

/* Poll only while the tab is visible. A farm ticking at 30 TPS does not need
   to be asked about while somebody is looking at the CS2 roster. */
document.querySelector('[data-tab="stardew"]').addEventListener('click', () => {
  sdvInit();
  sdvRefresh();
  clearInterval(sdvTimer);
  sdvTimer = setInterval(() => {
    if (document.querySelector('[data-tab="stardew"]').classList.contains('active')) sdvRefresh();
    else { clearInterval(sdvTimer); sdvTimer = null; }
  }, 6000);
});
