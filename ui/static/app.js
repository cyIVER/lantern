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
  // Bots carry synthetic 9007... ids that no admin command accepts, so they only
  // get a kick (issued by name via bot_kick).
  const acts = p.bot
    ? `<button class="btn tiny stop" data-act="kick">Kick Bot</button>`
    : `<button class="btn tiny" data-act="swap">Swap</button>
       <button class="btn tiny warn" data-act="slay">Slay</button>
       <button class="btn tiny warn" data-act="mute">Mute</button>
       <button class="btn tiny stop" data-act="kick">Kick</button>
       <button class="btn tiny kill" data-act="ban">Ban</button>`;
  tr.innerHTML = `
    <td>${p.bot ? '<span class="tag bot">BOT</span>' : '<span class="tag human">P</span>'}</td>
    <td>${escapeHtml(p.name)}</td>
    <td class="sid">${p.bot ? '<span style="color:#5d6b7c">—</span>' : sid}</td>
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
        body: { steamid64: sid || null, name: p.name, bot: !!p.bot, action, duration },
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
