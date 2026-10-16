import './style.css';
import * as THREE from 'three';
import { MarineViewer, type MarineData, type ColorMode, type PickLevel } from './MarineViewer';

// URL parameters:
//   manifest  manifest URL (default: bundled demo data)
//   api       API base URL; "none" for offline (default: same origin if /health answers)
const params = new URLSearchParams(location.search);
const manifestUrl = params.get('manifest') ?? './data/demo_osv/manifest.json';
const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

async function detectApi(): Promise<string | null> {
  const p = params.get('api');
  if (p === 'none') return null;
  const base = p ?? new URL('../', location.href).toString();
  try {
    const r = await fetch(new URL('health', base.endsWith('/') ? base : base + '/'), { signal: AbortSignal.timeout(1500) });
    if (r.ok && (await r.json()).ok) return base;
  } catch { /* offline */ }
  return null;
}

const fmt = {
  m: (v?: number) => (v === undefined || v === null ? '—' : `${v.toFixed(3)} m`),
  kg: (v?: number) => (v === undefined || v === null ? '—' : v >= 1000 ? `${(v / 1000).toFixed(2)} t` : `${v.toFixed(1)} kg`),
  short: (id?: string) => (id ? id.slice(0, 8) : '—'),
};

function esc(s: unknown): string {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!));
}

async function main() {
  const apiBase = await detectApi();
  const viewer = new MarineViewer($('viewport'), { manifestUrl, apiBase });
  (window as unknown as { viewer: MarineViewer }).viewer = viewer;
  try {
    await viewer.load().finally(() => undefined);
  } catch (e) {
    $('loading').textContent = (e as Error).message;
    return;
  }
  $('loading').hidden = true;
  const man = viewer.manifest;
  $('vessel-name').textContent = man.vessel.name;
  $('vessel-title').textContent = `${man.vessel.title}, asset v${man.vessel.assetVersion}${apiBase ? '' : ', offline (no API)'}`;
  document.title = `${man.vessel.name} assets`;

  // ---------------------------------------------------------------- tree
  const treeEl = $('tree');
  const filterEl = $<HTMLInputElement>('tree-filter');
  const renderTree = () => {
    const q = filterEl.value.trim().toLowerCase();
    const root = viewer.byId.get(man.vessel.id)!;
    treeEl.replaceChildren(nodeLi(root, q, 0) ?? document.createTextNode(''));
  };
  const matches = (m: MarineData, q: string) =>
    !q || [m.name, m.class, m.code].some((s) => String(s ?? '').toLowerCase().includes(q));
  const nodeLi = (o: THREE.Object3D, q: string, depth: number): HTMLLIElement | null => {
    const m = viewer.meta(o);
    const kids = viewer.assetChildren(o);
    const childLis = kids.map((k) => nodeLi(k, q, depth + 1)).filter(Boolean) as HTMLLIElement[];
    if (q && !matches(m, q) && childLis.length === 0) return null;
    const li = document.createElement('li');
    li.setAttribute('role', 'treeitem');
    const open = viewer.expanded.has(m.id!) || (!!q && childLis.length > 0);
    const row = document.createElement('div');
    row.className = 'node' + (viewer.selected && viewer.meta(viewer.selected).id === m.id ? ' is-selected' : '');
    row.style.setProperty('--depth', String(depth));
    const caret = document.createElement('button');
    caret.type = 'button';
    caret.className = 'caret';
    if (viewer.collapsible(o)) {
      caret.textContent = open ? '▾' : '▸';
      caret.setAttribute('aria-label', open ? `Collapse ${m.name}` : `Expand ${m.name}`);
      caret.onclick = (e) => { e.stopPropagation(); viewer.toggleExpanded(m.id!); };
    } else caret.disabled = true;
    const label = document.createElement('button');
    label.type = 'button';
    label.className = 'label';
    const count = m.role === 'asset' && kids.length && kids.every((k) => viewer.meta(k).role === 'instance') ? ` ×${kids.length}` : '';
    label.innerHTML = `<span class="nm">${esc(m.name)}${count}</span><span class="cls">${esc(m.class ?? '')}</span>`;
    label.onclick = () => viewer.select(o);
    label.ondblclick = () => viewer.fit(m.id);
    const eye = document.createElement('button');
    eye.type = 'button';
    eye.className = 'eye';
    const hidden = o.userData.userHidden === true;
    eye.textContent = hidden ? 'Show' : 'Hide';
    eye.onclick = (e) => { e.stopPropagation(); viewer.setHidden(m.id!, !hidden); };
    row.append(caret, label, eye);
    li.append(row);
    if (kids.length && open && childLis.length) {
      const ul = document.createElement('ul');
      ul.setAttribute('role', 'group');
      ul.append(...childLis);
      li.append(ul);
    }
    return li;
  };
  filterEl.oninput = renderTree;

  // ---------------------------------------------------------------- explode ladder
  const ladder = $('ladder-rows');
  const renderLadder = () => {
    const levels = viewer.explodeLevels();
    ladder.replaceChildren(...levels.map((d) => {
      const row = document.createElement('label');
      row.className = 'rung';
      const v = viewer.explode[d] ?? 0;
      row.innerHTML = `<span class="lvl">${viewer.levelName(d)}</span>
        <input type="range" min="0" max="1" step="0.01" value="${v}" aria-label="Explode ${viewer.levelName(d)} level" />
        <output>${Math.round(v * 100)}%</output>`;
      const input = row.querySelector('input')!;
      input.oninput = () => {
        viewer.setExplode(d, Number(input.value));
        row.querySelector('output')!.textContent = `${Math.round(Number(input.value) * 100)}%`;
      };
      return row;
    }));
  };
  $('btn-reset-explode').onclick = () => { viewer.explodeLevels().forEach((d) => viewer.setExplode(d, 0)); renderLadder(); };

  // ---------------------------------------------------------------- properties
  const propsEl = $('props');
  let propsFor: string | null = null;
  let statusTimer: number | undefined;
  const renderProps = async () => {
    const o = viewer.selected;
    if (!o) {
      propsFor = null;
      propsEl.innerHTML = '<p class="empty">Click a part in the view or the tree to see its properties.</p>';
      return;
    }
    const m = viewer.meta(o);
    if (propsFor === m.id) return;
    propsFor = m.id!;
    const parentSet = m.role === 'instance' && m.instanceOf ? viewer.byId.get(m.instanceOf) : null;
    const src = parentSet ? { ...viewer.meta(parentSet), ...m } : m;
    const rows: [string, string][] = [
      ['Class', esc(src.class)],
      ['Code', esc(src.code || '—')],
      ['Function', esc(src.function || '—')],
      ['Material', esc(src.material || '—')],
      ['Mass', `${fmt.kg(src.mass as number)}${src.massSource ? ` <span class="note">${esc(src.massSource)}</span>` : ''}`],
    ];
    if (src.diameter !== undefined) rows.push(['Diameter', fmt.m(src.diameter)]);
    if (src.length !== undefined) rows.push(['Length', fmt.m(src.length)]);
    if (src.thickness !== undefined) rows.push(['Thickness', fmt.m(src.thickness)]);
    if (src.geometryFidelity && src.geometryFidelity !== 'true') rows.push(['Geometry', esc(src.geometryFidelity)]);
    const wp = new THREE.Vector3();
    o.getWorldPosition(wp);
    const ship = viewer.shipRoot.worldToLocal(wp.clone());
    rows.push(['Origin (ship)', `x ${ship.x.toFixed(3)}, y ${ship.y.toFixed(3)}, z ${ship.z.toFixed(3)} m`]);
    const links = (ids?: string[]) => (ids ?? []).map((id) => {
      const t = viewer.byId.get(id);
      return t ? `<button type="button" class="link chip" data-id="${esc(id)}">${esc(viewer.meta(t).name)}</button>` : '';
    }).join(' ');
    if (src.connectedTo?.length) rows.push(['Connected to', links(src.connectedTo)]);
    if (src.serves?.length) rows.push(['Serves', links(src.serves)]);
    if (parentSet) rows.push(['Instance of', links([m.instanceOf!])]);
    propsEl.innerHTML = `
      <h2 class="p-name">${esc(m.name)}</h2>
      <p class="p-id" title="marine:id">${esc(m.id)}</p>
      <dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
      <div class="p-actions">
        <button type="button" id="p-fit">Zoom to</button>
        <button type="button" id="p-iso">${viewer.isolatedId === m.id ? 'Show all' : 'Isolate'}</button>
      </div>
      <section class="live" aria-live="polite"><h3>Live status</h3><div id="live-body">${apiBase ? 'Loading…' : '<p class="note">Live status needs the API. Start it with <code>make api</code> and open the viewer from it.</p>'}</div></section>`;
    propsEl.querySelectorAll<HTMLButtonElement>('[data-id]').forEach((b) => (b.onclick = () => viewer.selectId(b.dataset.id!)));
    $('p-fit').onclick = () => viewer.fit(m.id);
    $('p-iso').onclick = () => { viewer.isolate(viewer.isolatedId === m.id ? null : m.id!); propsFor = null; void renderProps(); };
    window.clearInterval(statusTimer);
    if (apiBase) {
      const load = async () => {
        try {
          const r = await fetch(viewer.api(`assets/${m.id}/status`));
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          const st = await r.json();
          const body = document.getElementById('live-body');
          if (!body || propsFor !== m.id) return;
          body.innerHTML = `
            <p class="cond cond-${esc(st.condition)}">Condition ${esc(st.condition)}<span class="note">, inspected ${esc(st.lastInspection)}</span></p>
            ${st.channels.length ? `<dl class="kv">${st.channels.map((c: { name: string; value: number; unit: string; iso19848: string }) =>
              `<dt title="${esc(c.iso19848)}">${esc(c.name)}</dt><dd>${c.value.toFixed(1)} ${esc(c.unit)}</dd>`).join('')}</dl>` : '<p class="note">No sensors on this item.</p>'}
            ${st.workOrders.map((w: { id: string; title: string; priority: string; due: string }) =>
              `<p class="wo">${esc(w.id)} ${esc(w.title)} <span class="note">${esc(w.priority)}, due ${esc(w.due)}</span></p>`).join('')}
            <p class="note">Simulated data source</p>`;
        } catch (e) {
          const body = document.getElementById('live-body');
          if (body) body.innerHTML = `<p class="note">Live status unavailable: ${esc((e as Error).message)}</p>`;
        }
      };
      void load();
      statusTimer = window.setInterval(load, 4000);
    }
  };

  // ---------------------------------------------------------------- toolbar
  $<HTMLSelectElement>('pick-level').onchange = (e) => { viewer.pickLevel = (e.target as HTMLSelectElement).value as PickLevel; };
  const colorSel = $<HTMLSelectElement>('color-mode');
  if (!apiBase) colorSel.querySelector<HTMLOptionElement>('option[value=status]')!.disabled = true;
  colorSel.onchange = () => viewer.setColorMode(colorSel.value as ColorMode);
  viewer.on(() => { if (colorSel.value !== viewer.colorMode) colorSel.value = viewer.colorMode; });
  const mBtn = $('btn-measure');
  mBtn.onclick = () => {
    const on = !viewer.measuring;
    viewer.setMeasuring(on);
    mBtn.setAttribute('aria-pressed', String(on));
    $('measure-out').hidden = !on;
    $('measure-out').textContent = 'Click two points on the model';
  };
  $('btn-fit').onclick = () => viewer.fit(viewer.isolatedId ?? undefined);
  $('btn-expand-all').onclick = () => viewer.setAllExpanded(true);
  $('btn-collapse-all').onclick = () => viewer.setAllExpanded(false);
  const panelsBtn = $('btn-panels');
  panelsBtn.onclick = () => {
    const open = document.body.classList.toggle('panels-open');
    panelsBtn.setAttribute('aria-expanded', String(open));
  };
  const secOn = $<HTMLInputElement>('section-on'), secX = $<HTMLInputElement>('section-x');
  const [x0, x1] = viewer.shipBoundsX();
  secX.min = x0.toFixed(2); secX.max = x1.toFixed(2); secX.value = ((x0 + x1) / 2).toFixed(2);
  const applySection = () => {
    secX.disabled = !secOn.checked;
    $('section-val').textContent = `${Number(secX.value).toFixed(2)} m`;
    viewer.setSection(secOn.checked ? Number(secX.value) : null);
  };
  secOn.onchange = applySection; secX.oninput = applySection; applySection();

  // ---------------------------------------------------------------- keep everything in step
  viewer.on(() => {
    renderTree();
    renderLadder();
    void renderProps();
    const lods = [...viewer.loadedLod.values()].join(', ');
    $('stats').textContent = `${viewer.drawnTriangles().toLocaleString()} triangles drawn, detail ${lods}`;
    if (viewer.measuring) $('measure-out').textContent = viewer.lastMeasure !== null
      ? `Distance ${viewer.lastMeasure.toFixed(3)} m` : 'Click two points on the model';
  });
  renderTree();
  renderLadder();
}

void main();
