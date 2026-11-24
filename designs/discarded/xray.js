/* RsHub · "X-ray" Home view: the laptop seen from above with a transparent shell.
   Fans that spin at the real rpm, heat pipes that go from cool to warm with the temperature,
   and every part can be pointed at to see its card. */
'use strict';
(() => {
const { $, $$, Spring, EASE, anim, tickers, esc, fmtInt } = SH;
const VW = 760, VH = 540;

/* ---------- dibujo ---------- */
const L = 'pathLength="1"';
function fan(cx, cy, id) {
  let blades = '';
  for (let i = 0; i < 11; i++) {
    const a = i * 360 / 11;
    blades += `<path d="M${cx} ${cy - 16} C ${cx + 14} ${cy - 30} ${cx + 30} ${cy - 50} ${cx + 18} ${cy - 66} C ${cx + 8} ${cy - 52} ${cx - 2} ${cy - 34} ${cx} ${cy - 16} Z" transform="rotate(${a.toFixed(2)} ${cx} ${cy})"/>`;
  }
  return `<g class="fan" data-fan="${id}">
    <circle ${L} class="ln housing" cx="${cx}" cy="${cy}" r="80"/>
    <path ${L} class="ln" d="M${cx - 80} ${cy} A80 80 0 0 1 ${cx - 40} ${cy - 69} L ${cx - 40} ${cy - 92} L ${cx + 76} ${cy - 92} L ${cx + 76} ${cy - 26}"/>
    <g class="rotor" style="transform-origin:${cx}px ${cy}px">
      <g class="blades">${blades}</g>
      <circle class="hub" cx="${cx}" cy="${cy}" r="15"/>
      <circle class="hubdot" cx="${cx}" cy="${cy}" r="4"/>
    </g>
  </g>`;
}
function fins(x, y, w) {
  let s = `<rect ${L} class="ln" x="${x}" y="${y}" width="${w}" height="26" rx="3"/>`;
  for (let i = 6; i < w; i += 6) s += `<line class="fin" x1="${x + i}" y1="${y + 3}" x2="${x + i}" y2="${y + 23}"/>`;
  return s;
}
function grille(x, y, w, h) {
  let s = ''; for (let yy = y + 12; yy < y + h - 6; yy += 9) for (let xx = x + 12; xx < x + w - 6; xx += 9) s += `<circle class="dot" cx="${xx}" cy="${yy}" r="1.6"/>`;
  return s;
}
function chips(x, y, n, w, h, gap) { let s = ''; for (let i = 0; i < n; i++) s += `<rect class="chipr" x="${(x + i * (w + gap)).toFixed(1)}" y="${y}" width="${w}" height="${h}" rx="1.5"/>`; return s; }
const PIPES = [
  { cls: 'cpu', d: 'M318 120 C 262 116 236 74 168 46' },
  { cls: 'cpu', d: 'M318 140 C 248 150 196 96 112 46' },
  { cls: 'gpu', d: 'M444 118 C 512 108 560 72 624 46' },
  { cls: 'gpu', d: 'M444 146 C 530 156 606 92 674 46' },
  { cls: 'mix', d: 'M334 104 C 372 70 410 70 440 102' },
];
function svg() {
  const pipe = (p, k) => `<path class="pipe-base" d="${p.d}"/><path class="pipe-cold" d="${p.d}"/><path class="pipe-warm ${p.cls}" data-pw="${k}" d="${p.d}"/><path class="pipe-flow" data-pf="${k}" pathLength="1" d="${p.d}"/>`;
  return `<svg class="xr-svg" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet" aria-label="Inside the laptop">
  <defs>
    <linearGradient id="xrScan" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--accent2)" stop-opacity="0"/><stop offset=".85" style="stop-color:var(--accent2)" stop-opacity=".16"/><stop offset="1" style="stop-color:var(--accent2)" stop-opacity=".75"/></linearGradient>
    <pattern id="xrGrid" width="20" height="20" patternUnits="userSpaceOnUse"><path d="M20 0H0V20" fill="none" style="stroke:var(--accent2)" stroke-opacity=".05" stroke-width="1"/></pattern>
    <clipPath id="xrClip"><rect x="10" y="10" width="740" height="520" rx="28"/></clipPath>
  </defs>
  <rect class="grid" x="10" y="10" width="740" height="520" rx="28" fill="url(#xrGrid)"/>
  <rect ${L} class="ln shell" x="10" y="10" width="740" height="520" rx="28"/>
  <rect ${L} class="ln inner" x="22" y="22" width="716" height="496" rx="20"/>

  <g class="ghost">
    <rect x="62" y="226" width="636" height="150" rx="10"/>
    <rect x="290" y="392" width="180" height="112" rx="10"/>
  </g>

  <g class="part" data-part="sys">
    <path ${L} class="ln board" d="M236 34 H524 V230 H700 V324 H60 V230 H236 Z"/>
    <rect class="chipr" x="476" y="196" width="24" height="18" rx="2"/><rect class="chipr" x="246" y="196" width="18" height="18" rx="2"/>
    <path class="trace" d="M364 132 H396 M364 150 H396 M300 174 V236 M330 174 V236 M444 180 V244"/>
    <rect class="hit" x="236" y="34" width="288" height="196"/>
  </g>

  <g class="part" data-part="display">
    <rect ${L} class="ln" x="66" y="12" width="86" height="18" rx="4"/><rect ${L} class="ln" x="608" y="12" width="86" height="18" rx="4"/>
    <path ${L} class="ln cable" d="M380 34 V22 H560 Q 600 22 606 18"/>
    <rect class="chipr" x="370" y="34" width="22" height="10" rx="2"/>
    <rect class="hit" x="60" y="8" width="640" height="26"/>
  </g>

  <g class="finstack">${fins(70, 22, 160)}${fins(530, 22, 160)}</g>
  ${fan(150, 150, 'cpu')}${fan(610, 150, 'gpu')}

  <g class="part" data-part="cpu">
    <rect ${L} class="ln pkg" x="282" y="92" width="82" height="82" rx="6"/>
    <rect class="die" x="299" y="110" width="48" height="44" rx="3"/>
    ${chips(288, 98, 3, 6, 4, 4)}
    <rect class="hit" x="282" y="92" width="82" height="82"/>
  </g>
  <g class="part" data-part="gpu">
    <rect ${L} class="ln pkg" x="396" y="84" width="96" height="96" rx="6"/>
    <rect class="die" x="418" y="106" width="52" height="52" rx="3"/>
    <rect class="vram" x="374" y="96" width="16" height="24" rx="2"/><rect class="vram" x="374" y="146" width="16" height="24" rx="2"/>
    <rect class="vram" x="498" y="96" width="16" height="24" rx="2"/><rect class="vram" x="498" y="146" width="16" height="24" rx="2"/>
    <rect class="hit" x="372" y="84" width="144" height="96"/>
  </g>

  <g class="pipes">${PIPES.map(pipe).join('')}</g>

  <g class="part" data-part="ram">
    <rect ${L} class="ln" x="250" y="238" width="152" height="32" rx="3"/>
    ${chips(260, 246, 8, 12, 16, 4.5)}
    <rect class="slot-empty" x="250" y="280" width="152" height="32" rx="3"/>
    <text class="tiny" x="326" y="300" text-anchor="middle">free slot</text>
    <rect class="hit" x="246" y="234" width="160" height="82"/>
  </g>
  <g class="part" data-part="ssd">
    <rect ${L} class="ln" x="428" y="246" width="144" height="34" rx="3"/>
    <rect class="chipr" x="440" y="253" width="26" height="20" rx="2"/><rect class="chipr" x="474" y="252" width="34" height="22" rx="2"/><rect class="chipr" x="514" y="252" width="34" height="22" rx="2"/>
    <circle class="screw" cx="562" cy="263" r="4"/>
    <rect class="hit" x="424" y="242" width="152" height="42"/>
  </g>
  <g class="part" data-part="wifi">
    <rect ${L} class="ln" x="600" y="244" width="46" height="40" rx="3"/>
    <rect class="chipr" x="609" y="254" width="16" height="16" rx="2"/>
    <path ${L} class="ln cable" d="M630 244 C 664 228 716 222 718 130 L 704 30"/>
    <path ${L} class="ln cable" d="M616 244 C 650 232 728 236 728 128 L 712 30"/>
    <rect class="hit" x="596" y="240" width="54" height="48"/>
  </g>
  <g class="part" data-part="battery">
    <rect ${L} class="ln" x="140" y="342" width="480" height="140" rx="14"/>
    ${[0, 1, 2, 3].map(i => `<rect class="cell" x="${150 + i * 117}" y="352" width="107" height="120" rx="9"/>`).join('')}
    <rect class="chipr" x="360" y="324" width="40" height="18" rx="2"/>
    <rect class="hit" x="140" y="324" width="480" height="158"/>
  </g>
  <g class="part" data-part="audio">
    <rect ${L} class="ln" x="34" y="362" width="84" height="112" rx="16"/>${grille(34, 362, 84, 112)}
    <rect ${L} class="ln" x="642" y="362" width="84" height="112" rx="16"/>${grille(642, 362, 84, 112)}
    <rect class="hit" x="34" y="362" width="84" height="112"/><rect class="hit" x="642" y="362" width="84" height="112"/>
  </g>
  <g class="ports">
    <rect x="8" y="238" width="10" height="16" rx="2"/><rect x="8" y="262" width="10" height="22" rx="2"/><rect x="8" y="292" width="10" height="12" rx="2"/><rect x="8" y="312" width="10" height="12" rx="2"/>
    <rect x="742" y="256" width="10" height="16" rx="2"/><rect x="742" y="282" width="10" height="12" rx="2"/>
  </g>
  <rect class="scan" x="10" y="-80" width="740" height="80" clip-path="url(#xrClip)" fill="url(#xrScan)"/>
</svg>`;
}

/* ---------- vista ---------- */
let root = null, offs = [], tick = null, hovered = null, leaveT = 0, litT = 0;
const fanState = { cpu: { a: 0, sp: new Spring(0, 30, 10) }, gpu: { a: 0, sp: new Spring(0, 30, 10) } };
const heat = { cpu: new Spring(0, 40, 12), gpu: new Spring(0, 40, 12) };
const card = { x: new Spring(0, 240, 26), y: new Spring(0, 240, 26), shown: false, right: true };
let flow = 0, anchor = { x: 0, y: 0 };

const heatOf = t => Math.max(0, Math.min(1, (t - 45) / 45));
const iconFor = id => ({ cpu: 'cpu', gpu: 'gpu', ram: 'ram', ssd: 'ssd', wifi: 'wifi', battery: 'battery', audio: 'audio', display: 'display', sys: 'sys' }[id] || 'sys');
function readout() {
  if (!root) return;
  const v = SH.tele.v;
  const set = (k, txt) => { const e = $(`[data-ro="${k}"]`, root); if (e) e.textContent = txt; };
  set('fcpu', fmtInt(v.fanCpu)); set('fgpu', fmtInt(v.fanGpu)); set('tcpu', Math.round(v.cpuTemp)); set('tgpu', Math.round(v.gpuTemp));
}

function mount(el, ctx) {
  root = document.createElement('div'); root.className = 'hv-xray'; el.appendChild(root);
  root.innerHTML = `<div class="xr-plan">${svg()}</div>
    <svg class="xr-lead" aria-hidden="true"><path class="lead" pathLength="1"/><circle class="lead-dot" r="3.5"/></svg>
    <div class="xr-card" role="tooltip"></div>
    <div class="xr-ro">
      <div><i class="sw c"></i>Processor fan <b><span data-ro="fcpu">0</span> rpm</b></div>
      <div><i class="sw g"></i>Graphics fan <b><span data-ro="fgpu">0</span> rpm</b></div>
      <div><i class="sw t"></i>Processor <b><span data-ro="tcpu">0</span> °C</b></div>
      <div><i class="sw t"></i>Graphics <b><span data-ro="tgpu">0</span> °C</b></div>
    </div>`;
  const svgEl = $('.xr-svg', root);
  $$('.part', svgEl).forEach(p => {
    const id = p.dataset.part;
    p.addEventListener('pointerenter', () => hover(id));
    p.addEventListener('pointerleave', unhover);
    p.addEventListener('click', () => ctx.openComponent(id));
  });
  const cardEl = $('.xr-card', root);
  cardEl.addEventListener('pointerenter', () => clearTimeout(leaveT));
  cardEl.addEventListener('pointerleave', unhover);
  cardEl.addEventListener('click', () => hovered && ctx.openComponent(hovered));

  offs.push(SH.tele.on(readout)); readout();

  const rotors = { cpu: $('[data-fan="cpu"] .rotor', svgEl), gpu: $('[data-fan="gpu"] .rotor', svgEl) };
  const warm = $$('[data-pw]', svgEl), flows = $$('[data-pf]', svgEl);
  tick = (dt) => {
    if (!root || (SH.state.view !== 'home' && !SH.homeAnimating)) return;
    const v = SH.tele.v, still = SH.state.settings.intensity === 'min';
    for (const k of ['cpu', 'gpu']) {
      const f = fanState[k]; f.sp.t = still ? 0 : (k === 'cpu' ? v.fanCpu : v.fanGpu) / 1500; f.sp.step(dt);
      if (still) continue;
      f.a = (f.a + f.sp.v * 360 * dt) % 360;
      rotors[k].style.transform = `rotate(${f.a.toFixed(2)}deg)`;
    }
    heat.cpu.t = heatOf(v.cpuTemp); heat.gpu.t = heatOf(v.gpuTemp); heat.cpu.step(dt); heat.gpu.step(dt);
    warm.forEach(p => { const c = p.classList.contains('cpu') ? heat.cpu.v : p.classList.contains('gpu') ? heat.gpu.v : (heat.cpu.v + heat.gpu.v) / 2; p.style.opacity = (.12 + .88 * c).toFixed(3); });
    if (!still) { flow -= dt * (.05 + .25 * Math.max(heat.cpu.v, heat.gpu.v)); flows.forEach(p => { p.style.strokeDashoffset = flow.toFixed(4); }); }
    card.x.step(dt); card.y.step(dt);
    if (card.shown) { cardEl.style.transform = `translate3d(${card.x.v.toFixed(1)}px,${card.y.v.toFixed(1)}px,0)`; drawLead(); }
  };
  tickers.add(tick);
  if (ctx.intro) intro(false); else svgEl.classList.add('lit');
}

function intro(quick) {
  const svgEl = root && $('.xr-svg', root); if (!svgEl) return;
  const scan = $('.scan', svgEl);
  const D = quick ? 700 : 1500;
  anim(scan, [{ transform: 'translateY(0px)' }, { transform: `translateY(${VH + 90}px)` }], { d: D, delay: quick ? 0 : 150, e: 'cubic-bezier(.45,.05,.35,1)' });
  fanState.cpu.sp.set(0); fanState.gpu.sp.set(0);
  if (!quick) {
    svgEl.classList.remove('lit'); clearTimeout(litT);
    const yOf = n => { try { return Math.max(0, n.getBBox().y); } catch { return 0; } };
    $$('.ln', svgEl).forEach(n => anim(n, [{ strokeDashoffset: 1 }, { strokeDashoffset: 0 }], { d: 900, delay: 150 + (yOf(n) / VH) * D * .9, e: EASE.out }));
    $$('.part > :not(.ln):not(.hit), .ports, .fan .rotor, .pipes, .ghost, .finstack line', svgEl).forEach(n =>
      anim(n, [{ opacity: 0 }, { opacity: 1 }], { d: 600, delay: 450 + (yOf(n) / VH) * D * .9, keep: true }));
    litT = setTimeout(() => svgEl.classList.add('lit'), (D + 300) * SH.motion());
    heat.cpu.set(0); heat.gpu.set(0);
  }
  $$('.xr-ro > div', root).forEach((d, i) => anim(d, [{ opacity: 0, transform: 'translate3d(0,8px,0)' }, { opacity: 1, transform: 'none' }], { d: 480, delay: (quick ? 100 : D) + i * 60, e: EASE.spring }));
}

/* ---------- card with leader line ---------- */
function hover(id) {
  clearTimeout(leaveT); if (hovered === id || !root) return;
  const comp = SH.PC.components.find(c => c.id === id); if (!comp) return;
  const was = hovered; hovered = id;
  const svgEl = $('.xr-svg', root); svgEl.classList.add('has-hot');
  $$('.part', svgEl).forEach(p => p.classList.toggle('hot', p.dataset.part === id));
  const cardEl = $('.xr-card', root);
  cardEl.innerHTML = `<div class="k">${SH.ic(iconFor(id))}${esc(comp.name)}</div><h3>${esc(comp.title)}</h3><dl>${comp.rows.map(r => `<dt>${esc(r[0])}</dt><dd>${esc(r[1])}</dd>`).join('')}</dl>${comp.bar ? `<div class="bar acc"><span style="transform:scaleX(0)"></span></div>` : ''}<div class="go">View in Live Monitor</div>`;
  placeCard(!was || !card.shown);
  card.shown = true; cardEl.classList.add('on');
  const lead = $('.xr-lead', root); lead.getAnimations().forEach(a => a.cancel()); lead.style.opacity = 1;
  drawLead();
  anim(cardEl, [{ opacity: was ? .5 : 0, filter: 'blur(6px)' }, { opacity: 1, filter: 'blur(0px)' }], { d: 300, e: EASE.out });
  anim($('.lead', root), [{ strokeDashoffset: 1 }, { strokeDashoffset: 0 }], { d: 420, e: EASE.out });
  anim($('.lead-dot', root), [{ opacity: 0 }, { opacity: 1 }], { d: 260 });
  const bar = $('.bar span', cardEl); if (bar) anim(bar, [{ transform: 'scaleX(0)' }, { transform: `scaleX(${comp.bar})` }], { d: 700, delay: 120 });
}
function unhover() {
  clearTimeout(leaveT);
  leaveT = setTimeout(() => {
    if (!hovered || !root) return; hovered = null;
    const svgEl = $('.xr-svg', root); svgEl.classList.remove('has-hot'); $$('.part', svgEl).forEach(p => p.classList.remove('hot'));
    const cardEl = $('.xr-card', root); cardEl.classList.remove('on');
    anim(cardEl, [{ opacity: 1, filter: 'blur(0px)' }, { opacity: 0, filter: 'blur(6px)' }], { d: 180 }).finished.then(() => { if (!hovered) card.shown = false; }).catch(() => {});
    anim($('.xr-lead', root), [{ opacity: 1 }, { opacity: 0 }], { d: 160 });
  }, 120);
}
function placeCard(snap) {
  const hit = root && $(`.part[data-part="${hovered}"] .hit`, root); if (!hit) return;
  const b = root.getBoundingClientRect(), r = hit.getBoundingClientRect();
  const W = root.clientWidth, H = root.clientHeight, cw = 248, ch = $('.xr-card', root).offsetHeight || 190;
  anchor = { x: r.left - b.left + r.width / 2, y: r.top - b.top + r.height / 2 };
  const right = anchor.x < W / 2;
  const reach = Math.min(r.width / 2, 80) + 46;
  let x = right ? anchor.x + reach : anchor.x - reach - cw;
  x = Math.max(6, Math.min(W - cw - 6, x));
  const y = Math.max(6, Math.min(H - ch - 54, anchor.y - 40));
  card.x.t = x; card.y.t = y; card.right = right;
  if (snap) { card.x.set(x); card.y.set(y); }
}
function drawLead() {
  if (!root) return;
  const cw = 248;
  const ex = card.right ? card.x.v : card.x.v + cw, ey = card.y.v + 40;
  const mx = card.right ? ex - 24 : ex + 24;
  $('.lead', root).setAttribute('d', `M${anchor.x.toFixed(1)} ${anchor.y.toFixed(1)} L ${mx.toFixed(1)} ${ey.toFixed(1)} L ${ex.toFixed(1)} ${ey.toFixed(1)}`);
  const dot = $('.lead-dot', root); dot.setAttribute('cx', anchor.x.toFixed(1)); dot.setAttribute('cy', anchor.y.toFixed(1));
}

function unmount() {
  if (tick) tickers.delete(tick); tick = null;
  offs.forEach(f => f()); offs = [];
  clearTimeout(leaveT); clearTimeout(litT); hovered = null; card.shown = false; root = null;
}

SH.homeVariant({
  id: 'xray', name: 'X-ray', short: 'X-ray',
  desc: 'The inside of the laptop, with live fans and heat.',
  mount, unmount,
  resize() { if (root && hovered) placeCard(true); },
  onReturn() { if (root) intro(true); },
});
})();
