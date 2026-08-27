/* LANtern landing page.
 *
 * Two jobs: show which game is running, and point at each game's own UI. The
 * game UIs are separate applications on their own URLs and are not embedded
 * here -- each carries its own theme and its own link back.
 *
 * ADDING A GAME UI
 *
 * Add a `ui` entry to the matching row in UIS below: a label and a URL builder.
 * That is the whole change; the card, the link and the state row are all driven
 * from the /api/servers response plus this map. Games with no UI simply have no
 * entry and get a panel link instead.
 */

const $  = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const UIS = {
  cs2: {
    ui: 'CS2 control',
    href: () => '/cs2',
    blurb: 'Maps, game modes, bots, match control and loadouts.',
  },
  // The Minecraft control UI is in progress. When it lands, give it a `ui`
  // label and an href -- nothing else here needs to change.
  minecraft: {
    blurb: 'Console, files and mods are in the Pelican panel for now.',
  },
  stardew: {
    ui: 'Stardew control',
    href: () => `http://${location.hostname}:8092/`,
    blurb: 'Farm controls, mod toggles and the game console.',
  },
};

const CONNECT = {
  cs2: () => `connect ${location.hostname}`,
  minecraft: () => `${location.hostname}:25565`,
  stardew: () => 'join by Steam invite code',
};

/* ------------------------------------------------------------------- utils */
function toast(msg, bad = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.toggle('bad', bad);
  t.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), 4200);
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
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
  if (!r.ok) {
    // FastAPI's `detail` may be an object -- the switcher returns one
    // describing what a start would have to stop. Stringifying it here is how
    // the user ends up reading "[object Object]".
    const d = data.detail;
    const err = new Error((typeof d === 'string' && d) || d?.message ||
                          data.raw || `HTTP ${r.status}`);
    err.detail = d;
    err.status = r.status;
    throw err;
  }
  return data;
}

/* ------------------------------------------------------------ confirmation */
function confirmDialog(message, okLabel = 'Stop it and start') {
  return new Promise((resolve) => {
    const back = $('#confirm-backdrop');
    $('#confirm-body').textContent = message;
    $('#confirm-ok').textContent = okLabel;
    back.hidden = false;

    const done = (answer) => {
      back.hidden = true;
      $('#confirm-ok').removeEventListener('click', yes);
      $('#confirm-cancel').removeEventListener('click', no);
      document.removeEventListener('keydown', onKey);
      back.removeEventListener('click', onBackdrop);
      resolve(answer);
    };
    const yes = () => done(true);
    const no  = () => done(false);
    const onKey = (e) => { if (e.key === 'Escape') done(false); };
    const onBackdrop = (e) => { if (e.target === back) done(false); };

    $('#confirm-ok').addEventListener('click', yes);
    $('#confirm-cancel').addEventListener('click', no);
    document.addEventListener('keydown', onKey);
    back.addEventListener('click', onBackdrop);
    $('#confirm-cancel').focus();
  });
}

/* ------------------------------------------------------------ game servers */
const STATE_PILL = {
  running: 'running', starting: 'starting', stopping: 'stopping',
  stopped: 'offline', absent: 'unknown', unknown: 'unknown',
};

let busy = false;

function serverCard(s) {
  const meta = UIS[s.id] || {};
  const el = document.createElement('article');
  el.className = 'server' + (s.state === 'running' ? ' is-running' : '');
  const label = s.state === 'stopped' ? 'offline' : s.state;

  el.innerHTML = `
    <div class="server-top">
      <h3>${escapeHtml(s.label)}</h3>
      <span class="pill ${STATE_PILL[s.state] || 'unknown'}">${escapeHtml(label)}</span>
    </div>
    <p class="server-blurb">${escapeHtml(meta.blurb || s.note || '')}</p>
    <p class="server-connect"></p>
    <div class="server-actions"></div>`;

  const connect = el.querySelector('.server-connect');
  if (s.state === 'running' && CONNECT[s.id]) {
    connect.textContent = CONNECT[s.id]();
  } else {
    connect.remove();
  }

  // Availability problems are worth reading -- "the Docker socket is not
  // mounted" is a fixable thing, not a mystery.
  if (!s.available && s.detail) {
    const warn = document.createElement('p');
    warn.className = 'server-warn';
    warn.textContent = s.detail;
    el.querySelector('.server-blurb').after(warn);
  }

  const actions = el.querySelector('.server-actions');
  const up = s.state === 'running' || s.state === 'starting';

  const btn = document.createElement('button');
  btn.className = 'btn tiny ' + (up ? 'stop' : 'go');
  btn.textContent = up ? 'Stop' : 'Start';
  btn.disabled = busy || (!up && !s.available);
  btn.addEventListener('click', () => (up ? stopGame(s.id, s.label) : startGame(s.id, s.label)));
  actions.append(btn);

  if (meta.ui) {
    const a = document.createElement('a');
    a.className = 'btn tiny open';
    a.href = meta.href();
    a.textContent = meta.ui;
    actions.append(a);
  }
  return el;
}

async function pollServers() {
  try {
    const d = await api('/api/servers');
    $('#server-list').replaceChildren(...d.servers.map(serverCard));
    $('#brand-sub').textContent = d.running.length
      ? `${d.servers.find((s) => s.id === d.running[0])?.label ?? d.running[0]} is up`
      : 'nothing running';
  } catch (e) {
    $('#server-list').innerHTML =
      `<div class="empty">Could not read server states: ${escapeHtml(e.message)}</div>`;
    $('#brand-sub').textContent = 'unreachable';
  }
}

async function startGame(id, label) {
  busy = true; await pollServers();
  try {
    let res;
    try {
      res = await api(`/api/servers/${id}/start`, { body: { confirm: false } });
    } catch (e) {
      // A 409 carrying needs_confirm is the server asking permission, not
      // failing. Show what it would stop, and only then send confirm.
      if (e.status === 409 && e.detail && e.detail.needs_confirm) {
        busy = false; await pollServers();
        if (!await confirmDialog(e.detail.message)) return;
        busy = true; await pollServers();
        res = await api(`/api/servers/${id}/start`, { body: { confirm: true } });
      } else throw e;
    }
    const also = (res.stopped || []).length ? ` Stopped ${res.stopped.join(', ')}.` : '';
    toast(`Starting ${label}.${also} Give it a minute.`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy = false; await pollServers();
  }
}

async function stopGame(id, label) {
  if (!await confirmDialog(
        `Stop ${label}? Anyone connected will be disconnected.`, 'Stop it')) return;
  busy = true; await pollServers();
  try {
    await api(`/api/servers/${id}/stop`, { body: {} });
    toast(`Stopping ${label}.`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy = false; await pollServers();
  }
}

/* -------------------------------------------------------------- host health */
function fmtUptime(sec) {
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

/* Colour by how worried to be, not by the raw number: 80% of a disk is fine,
   80% of memory on a box that is about to start an 11 GB server is not. */
function setGauge(id, percent, text, warnAt, badAt) {
  const el = $(id);
  const pct = Math.max(0, Math.min(100, percent || 0));
  const bar = el.querySelector('.bar span');
  bar.style.width = pct + '%';
  bar.className = pct >= badAt ? 'bad' : pct >= warnAt ? 'warn' : '';
  el.querySelector('.gval').textContent = text;
}

async function pollHost() {
  let h;
  try {
    h = await api('/api/host');
  } catch (e) {
    $('#host-sub').textContent = 'unreachable';
    return;
  }

  const m = h.memory;
  setGauge('#g-mem', m.percent, `${m.used_gb} / ${m.total_gb} GB used`, 75, 90);

  if (h.disk.available) {
    setGauge('#g-disk', h.disk.percent,
             `${h.disk.used_gb} / ${h.disk.total_gb} GB used`, 80, 92);
  } else {
    setGauge('#g-disk', 0, 'not readable from this container', 80, 92);
  }

  // Normalised to cores: 4.0 is idle on 12 cores and on fire on 2.
  const lpc = h.load_per_core;
  setGauge('#g-load', lpc * 100,
           `${h.load.map((x) => x.toFixed(2)).join('  ')}   over ${h.cores} cores`, 70, 100);

  const d = h.docker || {};
  $('#host-sub').textContent = d.hostname
    ? `${d.hostname} \u00b7 up ${fmtUptime(h.uptime_seconds)}`
    : `up ${fmtUptime(h.uptime_seconds)}`;

  const facts = [
    ['OS', d.os],
    ['Kernel', d.kernel],
    ['Docker', d.docker],
    ['Containers', d.available ? `${d.containers_running} running of ${d.containers_total}` : null],
  ].filter(([, v]) => v);

  $('#host-facts').replaceChildren(...facts.flatMap(([k, v]) => {
    const dt = document.createElement('dt'); dt.textContent = k;
    const dd = document.createElement('dd'); dd.textContent = v;
    return [dt, dd];
  }));
}

/* -------------------------------------------------------------------- boot */
$('#link-panel').href = `http://${location.hostname}/`;
$('#hostline').textContent = location.hostname;

pollServers();
pollHost();
setInterval(() => { if (!busy) pollServers(); }, 6000);
setInterval(pollHost, 10000);
