/* LANtern Stardew control -- front end.
 *
 * The whole page is a render of /api/overview, which never throws: a farm that
 * is still loading answers /health long before /status, and a half-populated
 * dashboard tells you far more than a single red error line.
 */

const $ = (s) => document.querySelector(s);

const TIMES = [
  [600, '6:00 am — sunrise'], [800, '8:00 am'], [1000, '10:00 am'],
  [1200, '12:00 pm — noon'], [1400, '2:00 pm'], [1600, '4:00 pm'],
  [1800, '6:00 pm — sunset'], [2000, '8:00 pm'], [2200, '10:00 pm'],
  [2400, '12:00 am'], [2600, '2:00 am — collapse'],
];

let timer = null;
let restartPending = false;

function toast(msg, bad = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.toggle('bad', bad);
  t.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), 4200);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    method: opts.method || (opts.body ? 'POST' : 'GET'),
  });
  const text = await r.text();
  let d;
  try { d = text ? JSON.parse(text) : {}; } catch { d = { raw: text }; }
  if (!r.ok) throw new Error(d.detail || d.raw || `HTTP ${r.status}`);
  return d;
}

function stat(dl, label, value) {
  const d = document.createElement('div');
  const dt = document.createElement('dt');
  const dd = document.createElement('dd');
  dt.textContent = label;
  dd.textContent = value;
  d.append(dt, dd);
  dl.appendChild(d);
}

function setState(kind, text) {
  const p = $('#pill-state');
  p.className = `pill pill-${kind}`;
  p.textContent = text;
}

/* --------------------------------------------------------------- overview */
async function refresh() {
  let d;
  try {
    d = await api('/api/overview');
  } catch (e) {
    setState('off', 'unreachable');
    $('#offline-msg').textContent = e.message;
    $('#offline').hidden = false;
    $('#body').hidden = true;
    return;
  }

  if (!d.configured) {
    setState('off', 'not configured');
    $('#offline-msg').textContent = d.note || 'Stardew is not configured.';
    $('#offline').hidden = false;
    $('#body').hidden = true;
    return;
  }

  if (!d.online) {
    setState('off', 'offline');
    $('#offline-msg').innerHTML =
      `The farm is not running.<br><span class="hint">Start it with ` +
      `<code>./lantern use stardew</code></span>`;
    $('#offline').hidden = false;
    $('#body').hidden = true;
    // Mods are readable from disk whether or not the game is up, but there is
    // nowhere sensible to show them while the rest of the page is hidden.
    return;
  }

  setState('on', 'farm online');
  $('#offline').hidden = true;
  $('#body').hidden = false;

  const st = d.status || {};
  const h = d.health || {};
  const set = (d.settings || {}).game || {};

  $('#invite').textContent = st.steamInviteCode || '——————';

  const dl = $('#stats');
  dl.replaceChildren();
  stat(dl, 'PLAYERS', `${st.playerCount ?? 0} / ${st.maxPlayers ?? '?'}`);
  stat(dl, 'FARM', set.farmName || '—');
  stat(dl, 'ENGINE', h.isFrozen ? 'frozen' : `${h.lastTickMs ?? '?'} ms`);
  stat(dl, 'TICKS', (h.tickCount ?? 0).toLocaleString());
  stat(dl, 'RENDER', (d.rendering?.fps ?? 0) > 0 ? `${d.rendering.fps} fps` : 'off');
  stat(dl, 'VERSION', (st.serverVersion || '—').split('-')[0]);

  renderPlayers(d);
  renderCabins(d);
  renderMods(d.mods, d.mod_problems);

  $('#fps').value = String(d.rendering?.fps ?? 0);
}

function renderPlayers(d) {
  const ps = (d.players?.players) || [];
  const tbl = $('#players');
  const tb = tbl.querySelector('tbody');
  tb.replaceChildren();
  $('#players-count').textContent = ps.length ? `${ps.length} online` : '';

  if (!ps.length) {
    tbl.hidden = true;
    $('#players-empty').hidden = false;
    return;
  }
  tbl.hidden = false;
  $('#players-empty').hidden = true;

  ps.forEach((p) => {
    const name = p.name || p.playerName || p.farmerName || '(unnamed)';
    const tr = document.createElement('tr');
    const td = (t) => { const c = document.createElement('td'); c.textContent = t; return c; };
    tr.append(td(name), td(p.isHost ? 'host (bot)' : 'farmhand'));

    const act = document.createElement('td');
    act.className = 'right';
    const b = document.createElement('button');
    b.className = 'btn btn-sm';
    b.textContent = 'Make admin';
    b.onclick = async () => {
      try { await api('/api/admin', { body: { name } }); toast(`${name} is now an admin.`); }
      catch (e) { toast(e.message, true); }
    };
    act.appendChild(b);
    tr.appendChild(act);
    tb.appendChild(tr);
  });
}

function renderCabins(d) {
  const c = d.cabins;
  $('#cabins').textContent = c
    ? `Cabins: ${c.assignedCount}/${c.totalCount} assigned, ${c.availableCount} free (${c.strategy})`
    : '';
}

/* ------------------------------------------------------------------- mods */
function renderMods(data, problems) {
  const box = $('#mods');
  box.replaceChildren();

  if (!data || !data.ok) {
    $('#mods-empty').hidden = false;
    $('#mods-empty').textContent = data?.error || 'Cannot read the mods folder.';
    $('#mods-count').textContent = '';
    return;
  }

  const list = data.mods || [];
  $('#mods-count').textContent = list.length
    ? `${data.enabled} enabled, ${data.disabled} disabled`
    : '';
  $('#mods-empty').hidden = list.length > 0;

  // SMAPI reports broken dependencies too, but only in a log nobody opens
  // until something is already wrong.
  const warn = $('#mods-warn');
  if (problems && problems.length) {
    warn.hidden = false;
    warn.innerHTML = '<strong>Missing dependencies.</strong> ' +
      problems.map((p) => `${p.mod} needs <code>${p.missing}</code>`).join(' · ');
  } else {
    warn.hidden = true;
  }

  list.forEach((m) => {
    const row = document.createElement('div');
    row.className = 'mod' + (m.enabled ? '' : ' off');

    const sw = document.createElement('label');
    sw.className = 'sw';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = m.enabled;
    cb.setAttribute('aria-label', `${m.enabled ? 'Disable' : 'Enable'} ${m.name}`);
    cb.onchange = () => toggleMod(m, cb);
    const knob = document.createElement('span');
    sw.append(cb, knob);

    const main = document.createElement('div');
    main.className = 'mod-main';
    const nm = document.createElement('div');
    nm.className = 'mod-name';
    nm.textContent = m.name;
    const meta = document.createElement('div');
    meta.className = 'mod-meta';
    meta.textContent = [m.version && `v${m.version}`, m.author && `by ${m.author}`]
      .filter(Boolean).join(' · ');
    main.append(nm, meta);
    if (m.description) {
      const de = document.createElement('div');
      de.className = 'mod-desc';
      de.textContent = m.description;
      de.title = m.description;
      main.appendChild(de);
    }

    row.append(sw, main);

    if (m.content_pack_for) {
      const t = document.createElement('span');
      t.className = 'tag';
      t.textContent = 'content pack';
      row.appendChild(t);
    }
    if (m.no_manifest || m.unparsable) {
      const t = document.createElement('span');
      t.className = 'tag tag-warn';
      t.textContent = m.no_manifest ? 'no manifest' : 'bad manifest';
      t.title = 'SMAPI will probably refuse to load this';
      row.appendChild(t);
    }

    box.appendChild(row);
  });
}

async function toggleMod(m, cb) {
  const want = cb.checked;
  cb.disabled = true;
  try {
    const r = await api('/api/mods/toggle', { body: { folder: m.folder, enabled: want } });
    toast(`${m.name} ${want ? 'enabled' : 'disabled'} — restart to apply.`);
    if (r.restart_required) {
      restartPending = true;
      $('#restart-bar').hidden = false;
    }
    await refresh();
  } catch (e) {
    cb.checked = !want;
    toast(e.message, true);
  } finally {
    cb.disabled = false;
  }
}

/* --------------------------------------------------------------- controls */
function init() {
  const sel = $('#time');
  TIMES.forEach(([v, label]) => sel.add(new Option(label, v)));
  sel.value = '900';

  $('#btn-refresh').onclick = () => refresh();

  $('#btn-copy').onclick = async () => {
    const code = $('#invite').textContent.trim();
    if (!code || code.startsWith('—')) return;
    try { await navigator.clipboard.writeText(code); toast('Invite code copied.'); }
    catch { toast('Could not copy — select it by hand.', true); }
  };

  $('#btn-time').onclick = async () => {
    try {
      await api('/api/time', { body: { value: Number($('#time').value) } });
      toast('Time set.');
      refresh();
    } catch (e) { toast(e.message, true); }
  };

  $('#btn-fps').onclick = async () => {
    const fps = Number($('#fps').value);
    try {
      await api('/api/rendering', { body: { fps } });
      toast(fps ? `Rendering at ${fps} fps.` : 'Rendering disabled.');
      refresh();
    } catch (e) { toast(e.message, true); }
  };

  $('#btn-reload').onclick = async () => {
    if (!confirm('Reload the world from server-settings.json? Players are briefly disconnected.')) return;
    try { await api('/api/reload', { body: {} }); toast('World reloaded.'); }
    catch (e) { toast(e.message, true); }
  };

  $('#btn-restart').onclick = async () => {
    if (!confirm('Restart the Stardew server? Everyone is disconnected for a couple of minutes.')) return;
    const b = $('#btn-restart');
    b.disabled = true;
    b.textContent = 'Restarting…';
    try {
      await api('/api/restart', { body: {} });
      toast('Restarting. The farm takes a minute or two to load.');
      restartPending = false;
      $('#restart-bar').hidden = true;
    } catch (e) {
      toast(e.message, true);
    } finally {
      b.disabled = false;
      b.textContent = 'Restart the server';
    }
  };

  $('#btn-shot').onclick = async () => {
    const img = $('#shot');
    try {
      const r = await fetch('/api/screenshot');
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      const blob = await r.blob();
      if (img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);
      img.src = URL.createObjectURL(blob);
      img.hidden = false;
    } catch (e) {
      toast(`${e.message} — set the render rate above 0 first.`, true);
    }
  };

  refresh();
  timer = setInterval(refresh, 8000);
}

document.addEventListener('visibilitychange', () => {
  // A farm ticking at 30 TPS does not need polling while the tab is buried.
  if (document.hidden) { clearInterval(timer); timer = null; }
  else if (!timer) { refresh(); timer = setInterval(refresh, 8000); }
});

init();
