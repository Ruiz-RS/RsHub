/* RsHub · "Exploded" Home view: the laptop in isometric, split into floating layers.
   Entrance: it arrives closed and comes apart with a spring. Hovering a layer moves the ones above aside so it can be seen. */
'use strict';
(() => {
const { $, $$, Spring, EASE, anim, tickers, ic, state, esc } = SH;
const W = 320, D = 210, C30 = Math.cos(Math.PI / 6);
const P = (x, y) => [+((x - y) * C30).toFixed(2), +((x + y) * .5).toFixed(2)];
const ISO = 'matrix(0.8660254,0.5,-0.8660254,0.5,0,0)';
const GAP = 72, UP = 84, DOWN = 46;

/* layers, bottom to top: t = visible thickness in px */
const LAYERS = [
  { id: 'bottom', name: 'Chassis and ventilation', desc: 'Two fans and rear vents.', t: 14, comps: [] },
  { id: 'battery', name: 'Battery', desc: 'A 90 Wh cell under the keyboard.', t: 12, comps: ['battery'] },
  { id: 'board', name: 'Motherboard', desc: 'Processor, graphics, memory, disk and Wi-Fi.', t: 5, comps: ['cpu', 'gpu', 'ram', 'ssd', 'wifi'] },
  { id: 'deck', name: 'Keyboard', desc: 'Backlit keyboard, touchpad and speakers.', t: 10, comps: ['audio'] },
  { id: 'lid', name: 'Screen', desc: 'A 16-inch panel and the system running on it.', t: 8, comps: ['display', 'sys'] },
];
const CLOSED = []; LAYERS.reduce((zz, L, i) => { CLOSED[i] = zz; return zz + L.t; }, 0);

/* ---------- dibujo ---------- */
function slab(L, topFill, sideA, sideB, content) {
  const p1 = P(W, 0), p2 = P(W, D), p3 = P(0, D), t = L.t;
  const fl = `${p3} ${p2} ${p2[0]},${p2[1] + t} ${p3[0]},${p3[1] + t}`;
  const fr = `${p1} ${p2} ${p2[0]},${p2[1] + t} ${p1[0]},${p1[1] + t}`;
  return `<g class="ly" data-ly="${L.id}"><g class="lyin">
    <polygon class="side" points="${fl}" fill="${sideA}"/><polygon class="side" points="${fr}" fill="${sideB}"/>
    <polyline class="edge" points="${p3} ${p2} ${p1}"/>
    <g transform="${ISO}"><rect class="top" width="${W}" height="${D}" rx="7" fill="${topFill}"/>${content}</g></g></g>`;
}
function zone(id, x, y, w, h, rx = 3) { return `<rect class="cz" data-comp="${id}" x="${x}" y="${y}" width="${w}" height="${h}" rx="${rx}"/>`; }
function fan(cx, cy, r, id) {
  let blades = '';
  for (let i = 0; i < 9; i++) blades += `<path transform="rotate(${i * 40} ${cx} ${cy})" d="M${cx} ${cy - 5} C ${cx + 7} ${cy - r * .45}, ${cx + 4} ${cy - r * .8}, ${cx - 3} ${cy - r + 3} L ${cx - 8} ${cy - r * .55} Z"/>`;
  return `<circle cx="${cx}" cy="${cy}" r="${r + 3}" fill="#070b14" stroke="rgba(170,200,245,.18)"/>
    <g class="fan" data-fan="${id}" fill="#26314a">${blades}</g><circle cx="${cx}" cy="${cy}" r="6" fill="#1a2236" stroke="rgba(170,200,245,.25)"/>`;
}
function lidContent() {
  let stars = ''; for (let i = 0; i < 26; i++) { const x = 20 + ((i * 97) % 280), y = 20 + ((i * 53) % 150); stars += `<circle cx="${x}" cy="${y}" r="${i % 5 ? .7 : 1.2}" fill="#cfe6ff" opacity="${.35 + (i % 4) * .15}"/>`; }
  return `<rect x="8" y="8" width="${W - 16}" height="${D - 16}" rx="6" fill="#060a13"/>
    <rect x="14" y="14" width="${W - 28}" height="${D - 34}" rx="3" fill="url(#exScr)"/>${stars}
    <ellipse cx="${W / 2}" cy="${D / 2 - 14}" rx="74" ry="26" fill="none" style="stroke:var(--accent2)" stroke-opacity=".45" transform="rotate(-10 ${W / 2} ${D / 2 - 14})"/>
    <circle cx="${W / 2}" cy="${D / 2 - 14}" r="12" style="fill:var(--accent)" opacity=".9"/><circle cx="${W / 2 + 62}" cy="${D / 2 - 26}" r="3.2" style="fill:var(--accent2)"/>
    <rect x="14" y="${D - 28}" width="${W - 28}" height="8" fill="#0d1526" opacity=".95"/>
    ${[0, 1, 2, 3, 4].map(i => `<rect x="${W / 2 - 24 + i * 10}" y="${D - 26.5}" width="6" height="5" rx="1" fill="#2a3a5a"/>`).join('')}
    <circle cx="${W / 2}" cy="11" r="1.4" fill="#1f2a40"/>
    ${zone('display', 14, 14, W - 28, D - 42)}${zone('sys', 14, D - 28, W - 28, 8, 1)}`;
}
function deckContent() {
  let keys = ''; const kx = 30, ky = 24, cols = 16, rows = 6, sx = (W - 60) / cols;
  for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
    if (r === 5 && c > 4 && c < 11) { if (c === 5) keys += `<rect x="${(kx + c * sx).toFixed(1)}" y="${ky + r * 17}" width="${(sx * 6 - 3).toFixed(1)}" height="13" rx="2" class="key"/>`; continue; }
    const hot = (r === 2 && c === 2) || (r === 3 && c >= 1 && c <= 3);
    keys += `<rect x="${(kx + c * sx).toFixed(1)}" y="${ky + r * 17}" width="${(sx - 3).toFixed(1)}" height="13" rx="2" class="key${hot ? ' hot' : ''}"/>`;
  }
  let dots = ''; for (const sx0 of [10, W - 20]) for (let i = 0; i < 6; i++) for (let j = 0; j < 2; j++) dots += `<circle cx="${sx0 + j * 6 + 2}" cy="${34 + i * 15}" r="1.6" fill="#0a0f1b"/>`;
  return `<rect x="${kx - 5}" y="${ky - 5}" width="${W - 50}" height="112" rx="5" fill="#090e19"/>
    <rect x="${kx - 3}" y="${ky - 3}" width="${W - 54}" height="108" rx="4" class="backlight"/>${keys}${dots}
    <rect x="${W / 2 - 56}" y="${D - 68}" width="112" height="54" rx="6" fill="#141b2c" stroke="rgba(170,200,245,.16)"/>
    ${zone('audio', 6, 28, 18, 92)}${zone('audio', W - 24, 28, 18, 92)}`;
}
function boardContent() {
  let tr = ''; for (let i = 0; i < 9; i++) { const y = 30 + i * 18; tr += `<path d="M${10 + (i % 3) * 6} ${y} H ${70 + i * 9} l 12 12 H ${W - 20 - (i % 4) * 8}"/>`; }
  const chips = (x, y) => [0, 1, 2, 3].map(i => `<rect x="${x + i * 21}" y="${y}" width="16" height="9" rx="1" fill="#0b111d"/>`).join('');
  return `<g class="traces">${tr}</g>
    <circle cx="45" cy="42" r="33" fill="#060a13" stroke="rgba(170,200,245,.14)"/><circle cx="${W - 45}" cy="42" r="33" fill="#060a13" stroke="rgba(170,200,245,.14)"/>
    <g class="pipes"><path d="M122 74 C 100 74, 80 60, 58 50"/><path d="M200 72 C 225 72, 245 60, ${W - 58} 50"/><path d="M122 84 C 150 98, 180 92, 200 82"/></g>
    <rect x="100" y="52" width="44" height="44" rx="3" fill="#1b2437" stroke="rgba(170,200,245,.22)"/><rect x="110" y="62" width="24" height="24" rx="2" fill="#2e3a56"/>
    <rect x="180" y="54" width="40" height="36" rx="3" fill="#232d44" stroke="rgba(170,200,245,.22)"/><rect x="188" y="61" width="24" height="22" rx="2" fill="#34405e"/>
    ${[[166, 52], [166, 76], [224, 52], [224, 76]].map(([x, y]) => `<rect x="${x}" y="${y}" width="10" height="14" rx="1" fill="#141c2d"/>`).join('')}
    <rect x="60" y="112" width="92" height="12" rx="2" fill="#18223a" stroke="rgba(170,200,245,.2)"/>${chips(64, 113.5)}
    <rect x="60" y="130" width="92" height="12" rx="2" fill="none" stroke="rgba(170,200,245,.18)" stroke-dasharray="3 3"/>
    <rect x="186" y="122" width="84" height="18" rx="2" fill="#1d2740" stroke="rgba(170,200,245,.2)"/><rect x="192" y="126" width="22" height="10" rx="1" fill="#0b111d"/><rect x="220" y="126" width="22" height="10" rx="1" fill="#0b111d"/><circle cx="264" cy="131" r="2.5" fill="#0b111d"/>
    <rect x="270" y="96" width="28" height="22" rx="2" fill="#1d2740" stroke="rgba(170,200,245,.2)"/><path class="ant" d="M276 96 C 276 80, 268 70, 262 60 M290 96 C 292 80, 300 72, 304 64"/>
    ${zone('cpu', 96, 48, 52, 52)}${zone('gpu', 162, 46, 76, 50)}${zone('ram', 56, 108, 100, 38)}${zone('ssd', 182, 118, 92, 26)}${zone('wifi', 266, 92, 36, 30)}`;
}
function batteryContent() {
  return `<rect x="36" y="112" width="248" height="84" rx="7" fill="url(#exBat)" stroke="rgba(170,200,245,.2)"/>
    <rect class="batfill" x="40" y="116" width="240" height="76" rx="5"/>
    ${[1, 2, 3].map(i => `<line x1="${36 + i * 62}" y1="116" x2="${36 + i * 62}" y2="192" stroke="rgba(170,200,245,.16)"/>`).join('')}
    <text x="52" y="160" class="pt">90 Wh</text><text x="52" y="176" class="pt sm">Li-ion, 4 cells</text>
    <rect x="60" y="26" width="200" height="60" rx="5" fill="none" stroke="rgba(170,200,245,.08)"/>
    ${zone('battery', 32, 108, 256, 92, 8)}`;
}
function bottomContent() {
  let slots = ''; for (let i = 0; i < 16; i++) slots += `<rect x="${(92 + i * 8.6).toFixed(1)}" y="16" width="4" height="40" rx="2" fill="#070b14"/>`;
  return `${slots}${fan(45, 42, 28, 'cpu')}${fan(W - 45, 42, 28, 'gpu')}
    ${[[22, 20], [W - 34, 20], [22, D - 26], [W - 34, D - 26]].map(([x, y]) => `<rect x="${x}" y="${y}" width="14" height="8" rx="4" fill="#0b101b"/>`).join('')}
    <rect x="40" y="${D - 60}" width="${W - 80}" height="30" rx="4" fill="none" stroke="rgba(170,200,245,.07)"/>`;
}
function stackSVG() {
  const fills = [['url(#exBottom)', '#0f1624', '#0a101b'], ['#121a2b', '#111827', '#0b111c'], ['url(#exPcb)', '#0d1626', '#0a111d'], ['url(#exDeck)', '#151d2f', '#0f1522'], ['url(#exLid)', '#161f31', '#10172a']];
  const content = [bottomContent(), batteryContent(), boardContent(), deckContent(), lidContent()];
  const sc = P(W / 2, D / 2);
  return `<svg class="ex-svg" viewBox="-215 -392 690 730" preserveAspectRatio="xMidYMid meet" aria-label="Exploded view of the PC">
    <defs>
      <linearGradient id="exLid" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#25304a"/><stop offset="1" stop-color="#161d2e"/></linearGradient>
      <linearGradient id="exDeck" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#232e45"/><stop offset="1" stop-color="#151c2c"/></linearGradient>
      <linearGradient id="exPcb" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#12203a"/><stop offset="1" stop-color="#0b1427"/></linearGradient>
      <linearGradient id="exBottom" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1c2537"/><stop offset="1" stop-color="#0f1522"/></linearGradient>
      <linearGradient id="exBat" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1c273d"/><stop offset="1" stop-color="#121a2a"/></linearGradient>
      <linearGradient id="exScr" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0c1830"/><stop offset=".6" stop-color="#101e38"/><stop offset="1" style="stop-color:color-mix(in oklab,var(--accent) 30%,#0c1426)"/></linearGradient>
      <radialGradient id="exShadow"><stop offset="0" stop-color="#000" stop-opacity=".7"/><stop offset="1" stop-color="#000" stop-opacity="0"/></radialGradient>
    </defs>
    <ellipse class="ex-shadow" cx="${sc[0]}" cy="${sc[1] + 44}" rx="300" ry="96" fill="url(#exShadow)"/>
    <g class="ex-stack">${LAYERS.map((l, i) => slab(l, ...fills[i], content[i])).join('')}</g>
    <g class="ex-labels">${LAYERS.map(l => `<g class="lab" data-ly="${l.id}"><line x1="0" y1="0" x2="46" y2="0"/><circle cx="0" cy="0" r="2.6"/><text x="56" y="4">${esc(l.name)}</text></g>`).join('')}</g>
  </svg>`;
}

/* ---------- vista ---------- */
let root = null, offs = [], timers = [], hov = -1, leaveT = 0, cardOn = false, mounted = false, exploded = false, unsubTele = null;
let els = {};
const z = LAYERS.map(() => new Spring(0, 120, 13));
const cardX = new Spring(0, 260, 26), cardY = new Spring(0, 260, 26);
const fanA = { cpu: 0, gpu: 0 };
const compById = id => SH.PC.components.find(c => c.id === id);

function targets() {
  LAYERS.forEach((L, i) => {
    let t = exploded ? i * GAP : CLOSED[i];
    if (exploded && hov >= 0) { if (i > hov) t += UP; else t -= DOWN - (i === hov ? 12 : 0); }
    z[i].t = t;
  });
}
function explode(stagger) {
  exploded = true; hov = -1;
  if (!stagger) { targets(); return; }
  // top to bottom: the lid comes off first, like lifting parts
  for (let i = LAYERS.length - 1; i >= 0; i--) timers.push(setTimeout(() => { if (hov < 0) z[i].t = i * GAP; }, (LAYERS.length - 1 - i) * 70 * SH.motion()));
}
function closeStack() { exploded = false; setHover(-1); targets(); }

function setHover(i) {
  clearTimeout(leaveT);
  if (hov === i || !root) return;
  hov = i; targets();
  $$('.ly', root).forEach((g, k) => g.classList.toggle('on', k === i));
  els.labs.forEach((g, k) => { g.classList.toggle('on', k === i); g.classList.toggle('dim', i >= 0 && k !== i); });
  showCard(i);
}
function leaveSoon() { clearTimeout(leaveT); leaveT = setTimeout(() => { setHover(-1); lightComp(null); }, 170); }

function cardHTML(L) {
  if (L.id === 'bottom') {
    const v = SH.tele.v;
    return `<div class="k">${esc(L.name)}</div><p>${esc(L.desc)}</p>
      <div class="fans"><div><b data-fanv="cpu">${SH.fmtInt(v.fanCpu)}</b><span>rpm, processor fan</span></div><div><b data-fanv="gpu">${SH.fmtInt(v.fanGpu)}</b><span>rpm, graphics fan</span></div></div>`;
  }
  const rows = L.comps.map(compById).filter(Boolean).map(c => `<button class="cr" data-comp="${c.id}"><span class="ci">${ic(c.id === 'sys' ? 'windows' : c.id)}</span><span class="ct"><b>${esc(c.name)}</b><span>${esc(c.title)}</span></span>${ic('chev')}</button>`).join('');
  return `<div class="k">${esc(L.name)}</div><p>${esc(L.desc)}</p><div class="crs">${rows}</div>`;
}
function showCard(i) {
  const card = els.card;
  if (i < 0) {
    if (!cardOn) return; cardOn = false; card.classList.remove('on');
    anim(card, [{ opacity: 1, filter: 'blur(0px)', transform: `translate3d(${cardX.v}px,${cardY.v}px,0) scale(1)` }, { opacity: 0, filter: 'blur(6px)', transform: `translate3d(${cardX.v}px,${cardY.v + 6}px,0) scale(.95)` }], { d: 180 });
    return;
  }
  card.innerHTML = cardHTML(LAYERS[i]);
  placeCard(i, !cardOn);
  card.classList.add('on');
  const a = anim(card, [{ opacity: cardOn ? .6 : 0, filter: 'blur(6px)', transform: `translate3d(${cardX.v}px,${cardY.v}px,0) scale(${cardOn ? .98 : .9})` }, { opacity: 1, filter: 'blur(0px)', transform: `translate3d(${cardX.v}px,${cardY.v}px,0) scale(1)` }], { d: 340, e: EASE.spring });
  a.finished.then(() => { if (cardOn && hov === i) { card.getAnimations().forEach(x => x.cancel()); card.style.opacity = 1; card.style.filter = 'none'; } }).catch(() => {});
  cardOn = true;
  $$('.cr', card).forEach((el, k) => anim(el, [{ opacity: 0, transform: 'translate3d(8px,0,0)' }, { opacity: 1, transform: 'none' }], { d: 320, delay: 60 + k * 40, e: EASE.spring }));
}
function placeCard(i, snap) {
  const lab = els.labs[i]; if (!lab) return;
  const r = lab.getBoundingClientRect(), w = root.getBoundingClientRect();
  const lx = r.left - w.left + 6, ly = r.top - w.top + r.height / 2;
  const cw = 264, ch = els.card.offsetHeight || 200;
  const x = Math.max(8, Math.min(root.clientWidth - cw - 8, lx)), y = Math.max(8, Math.min(root.clientHeight - ch - 8, ly - 22));
  cardX.t = x; cardY.t = y; if (snap) { cardX.set(x); cardY.set(y); }
}
function lightComp(id) {
  if (!root) return;
  $$('.cz', root).forEach(e => e.classList.toggle('lit', e.dataset.comp === id));
  $$('.cr', els.card).forEach(e => e.classList.toggle('on', e.dataset.comp === id));
}

function frame(dt, t) {
  if (!mounted || (state.view !== 'home' && !SH.homeAnimating)) return;
  const moving = state.settings.intensity !== 'min';
  const par = moving && state.settings.parallax ? 1 : 0;
  const mx = SH.mouse ? SH.mouse.x.v * par : 0, my = SH.mouse ? SH.mouse.y.v * par : 0;
  const corner = P(W, 0); // right corner: the label's leader line starts there
  LAYERS.forEach((L, i) => {
    z[i].step(dt);
    const breathe = moving && exploded && hov < 0 ? Math.sin(t / 900 + i * .7) * 3 : 0;
    const dx = mx * (i - 2) * 5, dy = my * (i - 2) * 2.5;
    const y = -(z[i].v + breathe) + dy;
    offs[i].setAttribute('transform', `translate(${dx.toFixed(2)},${y.toFixed(2)})`);
    els.labs[i].setAttribute('transform', `translate(${(corner[0] + 18 + dx).toFixed(2)},${(corner[1] + y).toFixed(2)})`);
  });
  // the shadow fades when exploding (the parts move away from the floor)
  const spread = Math.max(0, Math.min(1, z[LAYERS.length - 1].v / ((LAYERS.length - 1) * GAP)));
  els.shadow.setAttribute('transform', `translate(${(mx * -6).toFixed(2)},0)`);
  els.shadow.style.opacity = (1 - spread * .45).toFixed(3);
  if (moving) {
    const v = SH.tele.v;
    fanA.cpu = (fanA.cpu + dt * v.fanCpu * .09) % 360; fanA.gpu = (fanA.gpu + dt * v.fanGpu * .09) % 360;
    els.fans.forEach(f => { const cx = f.dataset.fan === 'cpu' ? 45 : W - 45; f.setAttribute('transform', `rotate(${fanA[f.dataset.fan].toFixed(1)} ${cx} 42)`); });
  }
  if (cardOn && hov >= 0) {
    placeCard(hov, false); cardX.step(dt); cardY.step(dt);
    if (!els.card.getAnimations().length) els.card.style.transform = `translate3d(${cardX.v.toFixed(2)}px,${cardY.v.toFixed(2)}px,0)`;
  }
}
function onTele(v) {
  if (!mounted) return;
  if (els.batfill) els.batfill.setAttribute('width', (240 * v.battery / 100).toFixed(1));
  if (cardOn && hov === 0) { const a = $('[data-fanv="cpu"]', els.card), b = $('[data-fanv="gpu"]', els.card); if (a) a.textContent = SH.fmtInt(v.fanCpu); if (b) b.textContent = SH.fmtInt(v.fanGpu); }
}

SH.homeVariant({
  id: 'exploded', name: 'Exploded view', short: 'Exploded',
  desc: 'Your PC in layers: hover one to see what is inside.',
  mount(el, ctx) {
    mounted = true;
    root = document.createElement('div'); root.className = 'hv-exploded'; el.appendChild(root);
    root.innerHTML = stackSVG() + '<div class="ex-card" role="dialog" aria-live="polite"></div><div class="ex-hint">Hover a layer</div>';
    offs = $$('.ly > .lyin', root);
    els = { card: $('.ex-card', root), shadow: $('.ex-shadow', root), labs: $$('.lab', root), fans: $$('.fan', root), batfill: $('.batfill', root), stack: $('.ex-stack', root) };
    $$('.ly', root).forEach((g, i) => { g.addEventListener('pointerenter', () => setHover(i)); g.addEventListener('pointerleave', leaveSoon); });
    els.labs.forEach((g, i) => {
      g.addEventListener('pointerenter', () => setHover(i)); g.addEventListener('pointerleave', leaveSoon);
      g.addEventListener('click', () => { const c = LAYERS[i].comps[0]; if (c) ctx.openComponent(c); });
    });
    root.addEventListener('pointerover', e => { const zc = e.target.closest('.cz, .cr'); if (zc) lightComp(zc.dataset.comp); });
    root.addEventListener('pointerout', e => {
      const zc = e.target.closest('.cz, .cr'); if (!zc) return;
      const to = e.relatedTarget && e.relatedTarget.closest ? e.relatedTarget.closest('.cz, .cr') : null;
      if (!to || to.dataset.comp !== zc.dataset.comp) lightComp(to ? to.dataset.comp : null);
    });
    root.addEventListener('click', e => { const zc = e.target.closest('.cz, .cr'); if (zc) ctx.openComponent(zc.dataset.comp); });
    els.card.addEventListener('pointerenter', () => clearTimeout(leaveT));
    els.card.addEventListener('pointerleave', leaveSoon);
    unsubTele = SH.tele.on(onTele); onTele(SH.tele.v);
    LAYERS.forEach((L, i) => z[i].set(CLOSED[i]));
    exploded = false; hov = -1;
    tickers.add(frame); frame(0, performance.now());
    if (ctx.intro) {
      // entrance: arrives as a closed laptop and explodes
      anim(els.stack, [{ opacity: 0, transform: 'translate(0px,-60px)', filter: 'blur(10px)' }, { opacity: 1, transform: 'translate(0px,0px)', filter: 'blur(0px)' }], { d: 700, delay: 150, e: EASE.soft });
      anim($('.ex-labels', root), [{ opacity: 0 }, { opacity: 1 }], { d: 500, delay: 1150 });
      anim($('.ex-hint', root), [{ opacity: 0, transform: 'translate3d(-50%,8px,0)' }, { opacity: 1, transform: 'translate3d(-50%,0,0)' }], { d: 500, delay: 1600, e: EASE.spring });
      timers.push(setTimeout(() => explode(true), 720 * SH.motion()));
    } else explode(false);
  },
  unmount() {
    mounted = false; tickers.delete(frame); if (unsubTele) unsubTele(); unsubTele = null;
    timers.forEach(clearTimeout); timers = []; clearTimeout(leaveT); hov = -1; cardOn = false; root = null; els = {}; offs = [];
  },
  resize() { if (hov >= 0) placeCard(hov, true); },
  onReturn() {
    if (!mounted) return;
    // on return: it closes for a moment and explodes again
    closeStack(); timers.push(setTimeout(() => explode(true), 260 * SH.motion()));
  },
});
})();
