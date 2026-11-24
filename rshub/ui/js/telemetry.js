/* RsHub · shared telemetry.
   Inside the app the data is REAL: Python sends it with SH.tele.push({...}) (rshub/engine/telemetry.py).
   Outside the app (browser, tests) it is simulated so the UI can be designed.
   What Windows does not provide arrives as null: the UI must show "—" (use SH.tele.fmt / SH.tele.has).
   Usage: SH.tele.v (current values), SH.tele.hist[key] (last 120), SH.tele.on(fn) (every update). */
'use strict';
(() => {
const rw = (v, lo, hi, j) => Math.max(lo, Math.min(hi, v + (Math.random() - .5) * j));
const v = {
  cpu: 14, cpuTemp: 61, cpuFreq: 3.9, threads: Array.from({ length: 24 }, () => .15),
  gpu: 8, gpuTemp: 52, gpuPower: 30, gpuClock: 1.2, vram: 3.1, vramTotal: 8, igpu: 4,
  ram: 9.4, ramTotal: 15.6, ramCache: 3.2, ramCommit: 14.1, procs: 280,
  diskRead: 40, diskWrite: 12, diskUsed: 592, diskTotal: 953, diskTemp: 41,
  down: 12, up: 1.4, ping: 18, wifiSignal: 82,
  battery: 100, charging: true, batteryPower: 0, batteryHealth: 97, fanCpu: 2600, fanGpu: 38, /* CPU fan in rpm; GPU fan in % (as NVML reports it) */
  uptime: 3 * 3600 + 1240,
};
const KEYS = ['cpu', 'cpuTemp', 'cpuFreq', 'gpu', 'gpuTemp', 'gpuPower', 'vram', 'ram', 'diskRead', 'diskWrite', 'down', 'up', 'ping', 'battery', 'batteryPower'];
const hist = {}; KEYS.forEach(k => hist[k] = Array(120).fill(v[k] ?? 0));
const subs = new Set();
const emit = () => subs.forEach(fn => { try { fn(v); } catch (e) { console.error(e); } });
const record = () => KEYS.forEach(k => { hist[k].push(v[k] ?? 0); hist[k].shift(); });

/* ---------- simulation (outside the app only) ---------- */
let burst = 0;
function tick() {
  if (Math.random() < .07) burst = 5;
  const b = burst > 0 ? (burst--, 26) : 0;
  v.cpu = Math.max(2, Math.min(99, rw(v.cpu * .85 + 12 * .15, 3, 96, 14) + b * .6));
  v.gpu = Math.max(0, Math.min(99, rw(v.gpu * .88 + 10 * .12, 1, 98, 12) + b));
  v.gpuTemp = rw(46 + v.gpu * .38, 38, 87, 2); v.gpuPower = rw(14 + v.gpu * 1.1, 6, 115, 4); v.gpuClock = rw(.9 + v.gpu / 100 * 1.6, .3, 2.6, .1);
  v.cpuFreq = rw(2.6 + v.cpu / 100 * 2.4, 2.2, 5.2, .2); v.cpuTemp = rw(50 + v.cpu * .34, 42, 94, 2);
  v.vram = rw(v.vram, 2.4, 6.8, .3); v.ram = rw(v.ram, 8.6, 11.8, .25); v.ramCommit = v.ram + 4.6; v.igpu = rw(v.igpu, 1, 30, 4);
  v.down = Math.max(.2, rw(v.down, .2, 180, Math.random() < .15 ? 90 : 10)); v.up = rw(v.up, .1, 24, 2); v.ping = rw(v.ping, 9, 60, 6);
  v.diskRead = rw(v.diskRead, 0, 900, Math.random() < .12 ? 600 : 40); v.diskWrite = rw(v.diskWrite, 0, 500, Math.random() < .1 ? 300 : 20);
  v.threads = v.threads.map((t, i) => Math.max(.02, Math.min(1, v.cpu / 100 + (Math.random() - .5) * .5 + (i < 16 ? .08 : -.04))));
  v.fanCpu = Math.round(rw(1800 + v.cpu * 32, 0, 5600, 120)); v.fanGpu = Math.round(rw(30 + v.gpu * .6, 0, 100, 3));
  v.uptime += .7;
  record(); emit();
}
let timer = setInterval(tick, 700);

/* ---------- real ---------- */
function goReal() {
  if (!SH.tele.simulated) return;
  SH.tele.simulated = false; clearInterval(timer); timer = null;
  /* nothing the simulation made up survives in the real app: until Python sends the first sample every value is
     "—" (null), never a sample number such as an uptime of 3 h 20 min */
  Object.keys(v).forEach(k => { v[k] = Array.isArray(v[k]) ? [] : typeof v[k] === 'boolean' ? false : null; });
  KEYS.forEach(k => hist[k].fill(0));
}
let firstReal = true;
function push(d) {
  goReal();
  for (const k in d) v[k] = d[k];
  /* the history started with sample values: the first real sample replaces all of it, so no chart begins with
     made-up numbers (a battery at 100 % on a laptop at 40 %, a sample GPU temperature) */
  if (firstReal) { firstReal = false; KEYS.forEach(k => hist[k].fill(v[k] ?? 0)); }
  record(); emit();
}
const has = k => v[k] !== null && v[k] !== undefined;
/* formats the value, or returns "—" if Windows does not provide it */
const fmt = (k, f = x => Math.round(x)) => has(k) ? f(v[k]) : '—';

/* ---------- lighter effects: on battery, or when the page draws on integrated graphics ----------
   Setting lightMode: 'auto' (default: on battery or integrated graphics only), 'always', 'never'. The older yes/no
   batterySaver still counts when lightMode was never chosen (core.js turns its "no" into 'never').
   SH.state.lite is what the rest of the UI reads (html[data-lite] for CSS); SH.on('power', fn) fires when it changes.
   Desktops report charging = null: they are never "on battery". SH.power.set(true|false|null) forces "on battery" (tests). */
let userRate = 1000, owner = false; /* only the main window sets the sampler's rate (the widget and the tray panel load this file too) */
const power = {
  onBattery: false, igpuOnly: false, forced: null,
  set(b) { power.forced = b === undefined ? null : b; apply(); },
};
/* the same rule as gpu_kind() in engine/telemetry.py: is this adapter a dedicated card? */
function gpuIsDiscrete(name) {
  const n = String(name || '').toLowerCase();
  if (!n || /remote display|virtual|parsec|indirect|basic display|basic render|citrix|vmware|virtualbox|hyper-v|spacedesk|displaylink|idd /.test(n)) return null;
  if (n.includes('nvidia')) return true;
  if (n.includes('intel')) return /arc(\(tm\))?\s+(pro\s+)?[ab]\d{3}/.test(n);
  if (n.includes('radeon') || n.includes('amd')) return /radeon(\(tm\))?\s*(rx|pro|vii|r[579]\b|hd\s*\d{4})|firepro|instinct/.test(n);
  return true;
}
/* Which adapter does the page really draw on? Ask the browser (the unmasked WebGL renderer names it) instead of guessing from
   the PC's parts: a laptop with a dedicated card can still render the window on the integrated chip. Software rendering counts
   as heavy too. If the browser will not say, fall back to Home's "Graphics" component (the dedicated card, else the only one). */
let renderIgpu;
function drawsOnIntegrated() {
  if (renderIgpu === undefined) {
    renderIgpu = null;
    try {
      const gl = document.createElement('canvas').getContext('webgl', { powerPreference: 'default' });
      const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
      const name = ext ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) || '') : '';
      if (/swiftshader|software|llvmpipe|basic render/i.test(name)) renderIgpu = true;
      else if (name) { const d = gpuIsDiscrete(name); renderIgpu = d === null ? null : !d; }
      if (gl) { const x = gl.getExtension('WEBGL_lose_context'); x && x.loseContext(); }
    } catch { renderIgpu = null; }
  }
  if (renderIgpu !== null) return renderIgpu;
  const g = ((SH.PC && SH.PC.components) || []).find(c => c && c.id === 'gpu');
  return !!g && gpuIsDiscrete(g.title) === false;
}
function apply() {
  if (!window.SH || !SH.state) return;
  const onB = power.forced !== null ? !!power.forced : v.charging === false;
  const igpu = drawsOnIntegrated();
  const S = SH.state.settings, mode = ['auto', 'always', 'never'].includes(S.lightMode) ? S.lightMode : (S.batterySaver === false ? 'never' : 'auto');
  const lite = mode === 'always' || (mode === 'auto' && (onB || igpu));
  const changed = lite !== SH.state.lite, rateChanged = onB !== power.onBattery;
  power.onBattery = onB; power.igpuOnly = igpu; SH.state.lite = lite;
  if (owner && (changed || rateChanged)) sendRate();
  if (!changed) return;
  document.documentElement.dataset.lite = lite ? '1' : '0';
  SH.emit('power', { lite, onBattery: onB, igpuOnly: igpu });
}
/* on battery the data refreshes at most every 2 s (and the sampler does less work, see Sampler.lite); on integrated graphics
   on AC only the drawing gets lighter, the data keeps its rate */
function sendRate() {
  const slow = SH.state.lite && power.onBattery;
  const ms = slow ? Math.max(2000, userRate) : userRate;
  SH.call('tele_rate', ms, !!slow);
  if (timer) { clearInterval(timer); timer = setInterval(tick, Math.max(250, ms * .7)); }
}
let lastCharging;
SH.tele = {
  v, hist, has, fmt, push, simulated: true, power,
  on(fn) { subs.add(fn); return () => subs.delete(fn); },
  setRate(ms) { userRate = +ms || 1000; if (owner) sendRate(); },
};
SH.power = power;
SH.tele.on(() => { if (v.charging !== lastCharging) { lastCharging = v.charging; apply(); } });
SH.on && SH.on('setting', d => { if (d.key === 'lightMode' || d.key === 'batterySaver') apply(); });
/* inside the app: real from the start (even if the first sample takes a moment) */
SH.apiReady.then(a => { if (a) goReal(); });
SH.on && SH.on('boot', () => { owner = true; apply(); SH.tele.setRate(+(SH.state.settings.teleRate || 1000)); });
})();
