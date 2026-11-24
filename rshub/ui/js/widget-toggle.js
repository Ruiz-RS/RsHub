/* RsHub · the widget from the main window: open/close (top bar, Home, Settings, palette),
   state kept in sync with the tray, and settings changes applied instantly. */
'use strict';
(() => {
const { $, $$, state } = SH;
SH.widgetOn = false;
function syncButtons() {
  $$('[data-widget-btn]').forEach(b => { b.classList.toggle('on', SH.widgetOn); b.setAttribute('aria-pressed', SH.widgetOn); });
  $$('.toggle[data-act="widget"]').forEach(t => t.classList.toggle('on', SH.widgetOn));
}
/* while the window is being created or closed, more clicks are ignored (a double click used to open and close it) */
let inFlight = false;
async function toggleWidget(quiet) {
  if (inFlight) return;
  SH.sound.claim();  /* its sound comes with the window (widget_on / widget_off), not with the click */
  inFlight = true; $$('[data-widget-btn], .toggle[data-act="widget"]').forEach(b => { b.setAttribute('aria-busy', 'true'); b.classList.add('busy'); });
  try {
    const sz = SH.widgetSize(state.settings);
    const r = await SH.call('widget_toggle', sz.w, sz.h);
    if (r === undefined) { SH.toast(SH.t('The widget only opens inside the app.'), { kind: 'unavailable' }); return; }
    SH.widgetOn = !!r; syncButtons();
    if (quiet !== true) SH.sound(r ? 'widget_on' : 'widget_off');
    if (quiet !== true) SH.toast(r ? SH.t('Widget open. Drag it wherever you like.') : SH.t('Widget closed.'), { group: 'widget' });
  } finally {
    inFlight = false; $$('[data-widget-btn], .toggle[data-act="widget"]').forEach(b => { b.removeAttribute('aria-busy'); b.classList.remove('busy'); });
  }
}
/* at startup: only if the setting asks for it or it was open when the app closed (off on a fresh install) */
SH.on('boot', () => { const s = state.settings; if (s.widgetWasOpen) /* remembered: open if it was left open */ setTimeout(() => SH.call('widget_is_open').then(v => { if (v === false) toggleWidget(true); }), 1500); });
SH.on('widget-mode', m => { const s = $('.seg[data-key="widgetMode"]'); if (s) { $$('button', s).forEach(x => x.classList.toggle('on', x.dataset.v === m)); SH.placeSeg(s); } });
SH.on('widget', d => { SH.widgetOn = !!(d && d.on); syncButtons(); });
SH.apiReady.then(() => SH.call('widget_is_open')).then(v => { SH.widgetOn = !!v; syncButtons(); });
/* any change that affects the widget is sent to it right away */
const KEYS = /^(widget|accent|theme|intensity)/;
SH.on('setting', ({ key }) => {
  if (!KEYS.test(key) || !SH.widgetOn) return;
  const s = state.settings;
  SH.call('widget_update', { widgetStats: s.widgetStats, widgetLayout: s.widgetLayout, widgetMode: s.widgetMode, accent: s.accent, accent2: s.accent2, theme: s.theme, intensity: s.intensity });
});
$('#widgetBtn').setAttribute('data-widget-btn', '');
$('#widgetBtn').addEventListener('click', toggleWidget);
document.addEventListener('click', e => { const b = e.target.closest('[data-widget-btn]'); if (b && b.id !== 'widgetBtn') toggleWidget(); });
SH.toggleWidget = toggleWidget; SH.syncWidgetButtons = syncButtons;
SH.actions.add([{ t: SH.t('Open or close the always-on-top widget'), run: toggleWidget, ic: 'monitor' }]);
})();
