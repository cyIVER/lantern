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

  // ui_up is absent for games whose UI is served by this very page, which is
  // trivially up if you are reading this.
  const uiUp = s.ui_up !== false;
  if (meta.ui && uiUp) {
    const a = document.createElement('a');
    a.className = 'btn tiny open';
    a.href = meta.href();
    a.textContent = meta.ui;
    actions.append(a);
  } else if (meta.ui) {
    const dead = document.createElement('button');
    dead.className = 'btn tiny';
    dead.textContent = meta.ui;
    dead.disabled = true;
    dead.title = 'That UI is not running. Start the game and it comes up with it.';
    actions.append(dead);
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

/* Rolling history for the sparklines. Client-side only -- the server keeps no
   series, so a reload starts the graphs over. That is honest: this is a "what
   is it doing right now" panel, not monitoring. */
const HISTORY = 40;
const series = { cpu: [], mem: [] };

function pushSeries(key, value) {
  if (value === null || value === undefined) return;
  const a = series[key];
  a.push(value);
  if (a.length > HISTORY) a.shift();
}

function drawSpark(sel, key) {
  const line = $(sel).querySelector('polyline');
  const a = series[key];
  if (a.length < 2) { line.setAttribute('points', ''); return; }
  // Fixed 0-100 scale, not auto-scaled: an auto-scaled graph of a machine
  // doing nothing looks identical to one that is on fire.
  const step = 100 / (HISTORY - 1);
  const pts = a.map((v, i) =>
    `${((i + (HISTORY - a.length)) * step).toFixed(1)},${(28 - (v / 100) * 26 - 1).toFixed(1)}`);
  line.setAttribute('points', pts.join(' '));
}

/* Colour by how worried to be, not by the raw number: 80% of a disk is fine,
   80% of memory on a box about to start an 11 GB server is not. */
function tone(pct, warnAt, badAt) {
  return pct >= badAt ? 'bad' : pct >= warnAt ? 'warn' : 'ok';
}

function setStat(sel, { big, sub, foot, pct, warnAt, badAt }) {
  const el = $(sel);
  el.querySelector('.stat-big').textContent = big;
  if (sub !== undefined) el.querySelector('.stat-sub').textContent = sub;
  const f = el.querySelector('.stat-foot');
  if (f && foot !== undefined) f.textContent = foot;

  if (pct !== undefined && pct !== null) {
    const t = tone(pct, warnAt, badAt);
    el.dataset.tone = t;
    const bar = el.querySelector('.bar span');
    if (bar) {
      bar.style.width = Math.max(0, Math.min(100, pct)) + '%';
      bar.className = t;
    }
  } else {
    el.dataset.tone = 'ok';
  }
}

function containerRow(c) {
  const el = document.createElement('div');
  el.className = 'crow' + (c.state === 'running' ? '' : ' is-down');
  el.innerHTML = `
    <span class="cdot"></span>
    <span class="cname"></span>
    <span class="cstatus"></span>`;
  el.querySelector('.cname').textContent = c.name;
  el.querySelector('.cstatus').textContent = c.status || c.state;
  el.title = c.image || '';
  return el;
}

async function pollHost() {
  let h;
  try {
    h = await api('/api/host');
  } catch {
    $('#host-sub').textContent = 'unreachable';
    return;
  }

  // CPU is a rate and has no value until the second poll.
  pushSeries('cpu', h.cpu_percent);
  setStat('#s-cpu', {
    big: h.cpu_percent === null ? '\u2014' : `${h.cpu_percent}%`,
    sub: `${h.cores} cores`,
    pct: h.cpu_percent, warnAt: 70, badAt: 90,
  });
  drawSpark('#s-cpu', 'cpu');

  const m = h.memory;
  pushSeries('mem', m.percent);
  setStat('#s-mem', {
    big: `${m.used_gb} GB`,
    sub: `of ${m.total_gb} GB`,
    pct: m.percent, warnAt: 75, badAt: 90,
  });
  drawSpark('#s-mem', 'mem');

  if (h.disk.available) {
    setStat('#s-disk', {
      big: `${h.disk.used_gb} GB`,
      sub: `of ${h.disk.total_gb} GB`,
      pct: h.disk.percent, warnAt: 80, badAt: 92,
    });
  } else {
    setStat('#s-disk', { big: '\u2014', sub: 'not readable' });
  }

  // Normalised to cores: 4.0 is idle on twelve cores and on fire on two.
  setStat('#s-load', {
    big: h.load[0].toFixed(2),
    sub: `${h.load[1].toFixed(2)} / ${h.load[2].toFixed(2)}`,
    pct: h.load_per_core * 100, warnAt: 70, badAt: 100,
  });

  const sw = h.swap;
  setStat('#s-swap', {
    big: sw.total_gb ? `${sw.used_gb} GB` : 'none',
    sub: sw.total_gb ? `of ${sw.total_gb} GB` : 'not configured',
    // Swap in use on a game host means memory pressure already happened.
    pct: sw.total_gb ? sw.percent : 0, warnAt: 1, badAt: 25,
  });

  const d = h.docker || {};
  setStat('#s-up', {
    big: fmtUptime(h.uptime_seconds),
    sub: d.hostname || '',
    foot: d.available ? `${d.containers_running} of ${d.containers_total} containers up` : '',
  });

  $('#host-sub').textContent = d.hostname
    ? `${d.hostname} \u00b7 ${d.os || ''}`.trim()
    : `up ${fmtUptime(h.uptime_seconds)}`;

  $('#containers').replaceChildren(
    ...(d.containers || []).map(containerRow));

  const facts = [
    ['OS', d.os],
    ['Kernel', d.kernel],
    ['Docker', d.docker],
    ['Images', d.images],
    ['Memory cached', m.cached_gb ? `${m.cached_gb} GB` : null],
    ['Disk free', h.disk.available ? `${h.disk.free_gb} GB` : null],
  ].filter(([, v]) => v !== null && v !== undefined && v !== '');

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
// 5s: the CPU figure is the average over the gap, so a long gap smooths away
// exactly the spikes worth seeing.
setInterval(pollHost, 5000);
