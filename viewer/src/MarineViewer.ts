// Embeddable marine asset viewer.
//
// Driven entirely by a vessel manifest and the stable IDs in each GLB chunk:
//   node.userData.marine.role = "asset" | "instance" | "geom" | "proxy"
//   asset/instance nodes carry .id; geom/proxy nodes carry .of (owner id)
// Adding a vessel or a system needs no code change here.
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/examples/jsm/libs/meshopt_decoder.module.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';

export type Lod = 'L0' | 'L1' | 'L2';

export interface Chunk {
  system: string; systemName: string; lod: Lod; url: string; href?: string;
  bytes: number; triangles: number; bbox: [number[], number[]] | null;
}
export interface TreeEntry {
  id: string; name: string; class: string; code: string; kind: string; function: string; mass: number;
  translation: number[]; rotation: number[]; explodeDir: number[]; explodeDist: number;
  children: TreeEntry[]; system?: boolean;
}
export interface Manifest {
  schemaVersion: string;
  vessel: { id: string; name: string; title: string; assetVersion: string };
  frame: Record<string, unknown>;
  tree: TreeEntry; chunks: Chunk[];
}
export interface MarineData {
  role: 'asset' | 'instance' | 'geom' | 'proxy';
  id?: string; of?: string; name?: string; class?: string; kind?: string; code?: string;
  function?: string; material?: string; mass?: number; diameter?: number; length?: number; thickness?: number;
  explodeDir?: number[]; explodeDist?: number; instanceOf?: string; definitionId?: string;
  connectedTo?: string[]; serves?: string[]; massSource?: string; geometryFidelity?: string; path?: string;
  [k: string]: unknown;
}
export type ColorMode = 'material' | 'equipment' | 'status';
export type PickLevel = 'part' | 'component' | 'equipment' | 'system';

export interface ViewerOptions {
  manifestUrl: string;
  apiBase?: string | null;          // e.g. "/" when served by the API; null = offline
  autoDetail?: boolean;             // stream L2 right after L0
}

type Listener = () => void;

/** Single-file mode: manifest and GLB chunks inlined as base64 (see scripts/single-file.mjs). */
interface Embedded { manifest: Manifest; chunks: Record<string, string> }
const embedded = (globalThis as unknown as { __MARINE_EMBED__?: Embedded }).__MARINE_EMBED__;

function b64ToBuffer(b64: string): ArrayBuffer {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

const LEVEL_NAMES = ['Vessel', 'Zone', 'System', 'Equipment', 'Component', 'Part'];

export class MarineViewer {
  readonly renderer: THREE.WebGLRenderer;
  readonly scene = new THREE.Scene();
  readonly camera = new THREE.PerspectiveCamera(40, 1, 0.05, 2000);
  readonly controls: OrbitControls;
  readonly shipRoot = new THREE.Group();        // Z-up ship frame -> Y-up, applied once here
  manifest!: Manifest;
  readonly byId = new Map<string, THREE.Object3D>();
  readonly expanded = new Set<string>();
  readonly explode: number[] = [0, 0, 0, 0, 0, 0]; // slider per hierarchy depth
  selected: THREE.Object3D | null = null;
  pickLevel: PickLevel = 'part';
  colorMode: ColorMode = 'material';
  readonly loadedLod = new Map<string, Lod>();
  readonly status = new Map<string, { condition: string }>();
  private systemSlots = new Map<string, THREE.Object3D>();
  private loader = new GLTFLoader();
  private listeners = new Set<Listener>();
  private needsRender = true;
  private clip = new THREE.Plane(new THREE.Vector3(-1, 0, 0), 1e6);
  private highlightMats = new WeakMap<THREE.Material, THREE.Material>();
  private overrideMats = new Map<string, THREE.MeshStandardMaterial>();
  private measurePts: THREE.Vector3[] = [];
  private measureGroup = new THREE.Group();
  measuring = false;
  lastMeasure: number | null = null;
  readonly opts: ViewerOptions;

  constructor(readonly container: HTMLElement, opts: ViewerOptions) {
    this.opts = { autoDetail: true, ...opts };
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.localClippingEnabled = true;
    this.renderer.clippingPlanes = [];
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    container.appendChild(this.renderer.domElement);
    const pm = new THREE.PMREMGenerator(this.renderer);
    this.scene.environment = pm.fromScene(new RoomEnvironment(), 0.04).texture;
    this.scene.add(new THREE.HemisphereLight(0xdfe8ef, 0x5b6670, 0.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.4);
    sun.position.set(20, 30, 15);
    this.scene.add(sun);
    this.shipRoot.rotation.x = -Math.PI / 2;
    this.scene.add(this.shipRoot);
    this.scene.add(this.measureGroup);
    const grid = new THREE.GridHelper(60, 60, 0x9aa8b4, 0xc9d1d8);
    grid.position.set(14, 0, 0);
    (grid.material as THREE.Material).transparent = true;
    (grid.material as THREE.Material).opacity = 0.5;
    this.scene.add(grid);
    this.loader.setMeshoptDecoder(MeshoptDecoder);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.addEventListener('change', () => this.requestRender());
    new ResizeObserver(() => this.resize()).observe(container);
    this.resize();
    this.renderer.domElement.addEventListener('pointerdown', (e) => this.onPointerDown(e));
    this.renderer.domElement.addEventListener('pointerup', (e) => this.onPointerUp(e));
    const loop = () => {
      requestAnimationFrame(loop);
      const moved = this.controls.update();
      if (this.needsRender || moved) {
        this.renderer.render(this.scene, this.camera);
        this.needsRender = false;
      }
    };
    loop();
  }

  on(fn: Listener) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  private emit() { this.listeners.forEach((f) => f()); }
  requestRender() { this.needsRender = true; }

  private resize() {
    const w = this.container.clientWidth || 1, h = this.container.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.requestRender();
  }

  // ------------------------------------------------------------ loading
  async load(): Promise<void> {
    if (embedded) this.manifest = embedded.manifest;
    else {
      const res = await fetch(this.opts.manifestUrl);
      if (!res.ok) throw new Error(`Manifest not found at ${this.opts.manifestUrl} (HTTP ${res.status}). Run the build first.`);
      this.manifest = await res.json();
    }
    this.shipRoot.add(this.buildSkeleton(this.manifest.tree, 0));
    // collapse rules start from: vessel, zones and systems expanded
    const markOpen = (e: TreeEntry) => { this.expanded.add(e.id); if (!e.system) e.children.forEach(markOpen); };
    markOpen(this.manifest.tree);
    await Promise.all([...this.systemSlots.keys()].map((id) => this.loadSystem(id, 'L0')));
    this.fit();
    this.emit();
    if (this.opts.autoDetail) {
      for (const id of this.systemSlots.keys()) await this.loadSystem(id, 'L2', true);
    }
  }

  private buildSkeleton(e: TreeEntry, depth: number): THREE.Object3D {
    const o = new THREE.Group();
    o.name = e.id;
    o.position.fromArray(e.translation);
    o.quaternion.fromArray(e.rotation);
    o.userData.marine = { role: 'asset', id: e.id, name: e.name, class: e.class, code: e.code, kind: e.kind,
      function: e.function, mass: e.mass, explodeDir: e.explodeDir, explodeDist: e.explodeDist } as MarineData;
    o.userData.basePos = o.position.clone();
    o.userData.depth = depth;
    o.userData.skeleton = true;
    this.byId.set(e.id, o);
    if (e.system) this.systemSlots.set(e.id, o);
    else e.children.forEach((c) => o.add(this.buildSkeleton(c, depth + 1)));
    return o;
  }

  chunkUrl(c: Chunk): string {
    if (c.href) return new URL(c.href, new URL(this.opts.manifestUrl, location.href)).toString();
    return new URL(c.url, new URL(this.opts.manifestUrl, location.href)).toString();
  }

  async loadSystem(systemId: string, lod: Lod, expandAll = false): Promise<void> {
    if (this.loadedLod.get(systemId) === lod) return;
    const chunk = this.manifest.chunks.find((c) => c.system === systemId && c.lod === lod);
    if (!chunk) return;
    const gltf = embedded?.chunks[chunk.url]
      ? await this.loader.parseAsync(b64ToBuffer(embedded.chunks[chunk.url]), '')
      : await this.loader.loadAsync(this.chunkUrl(chunk));
    const slot = this.systemSlots.get(systemId)!;
    for (const old of [...slot.children]) {
      if (old.userData.chunk) { slot.remove(old); this.unindex(old); }
    }
    const root = gltf.scene;
    root.userData.chunk = { lod, systemId };
    slot.add(root);
    const slotDepth = slot.userData.depth as number;
    root.traverse((o) => {
      const m = o.userData.marine as MarineData | undefined;
      if (o instanceof THREE.Mesh) {
        o.userData.origMaterial = o.material;
        const mats = Array.isArray(o.material) ? o.material : [o.material];
        mats.forEach((mm) => { mm.clippingPlanes = [this.clip]; mm.clipShadows = true; });
      }
      if (!m) return;
      if (m.role === 'asset' || m.role === 'instance') {
        const id = (m.id ?? o.name) as string;
        // the chunk's root node is the system itself: keep the skeleton slot as its transform owner
        o.userData.basePos = o.position.clone();
        if (id === systemId) {
          // the chunk's root node is the system itself; the skeleton slot owns its transform
          o.userData.isChunkRoot = true;
          o.userData.depth = slotDepth;
          slot.userData.chunkRoot = o;
          Object.assign(slot.userData.marine, m);
          this.byId.set(id, slot);
        } else {
          this.byId.set(id, o);
        }
      }
    });
    // depth = slot depth + number of asset ancestors between the node and the slot
    root.traverse((o) => {
      const m = o.userData.marine as MarineData | undefined;
      if (!m || (m.role !== 'asset' && m.role !== 'instance') || o.userData.isChunkRoot) return;
      let d = slotDepth, p = o.parent;
      while (p && p !== slot) { if ((p.userData.marine as MarineData | undefined)?.role === 'asset') d++; p = p.parent; }
      o.userData.depth = d;
    });
    this.loadedLod.set(systemId, lod);
    if (expandAll && lod === 'L2') {
      root.traverse((o) => {
        const m = o.userData.marine as MarineData | undefined;
        if (m?.role === 'asset' && m.id && this.collapsible(o)) this.expanded.add(m.id);
      });
    }
    this.applyExplode();
    this.applyVisibility();
    this.applyColors();
    if (this.selected) {
      const id = (this.selected.userData.marine as MarineData).id!;
      this.select(this.byId.get(id) ?? null);
    }
    this.requestRender();
    this.emit();
  }

  private unindex(root: THREE.Object3D) {
    root.traverse((o) => {
      const m = o.userData.marine as MarineData | undefined;
      if (m?.id && this.byId.get(m.id) === o) this.byId.delete(m.id);
    });
  }

  // ------------------------------------------------------------ structure helpers
  /** The object that holds an asset's children (system slot -> its chunk root). */
  private holder(o: THREE.Object3D): THREE.Object3D { return (o.userData.chunkRoot as THREE.Object3D) ?? o; }

  /** True when the node can be collapsed to a proxy (or has children). */
  collapsible(o: THREE.Object3D): boolean {
    return this.assetChildren(o).length > 0 || this.partsOf(o, 'proxy').length > 0;
  }

  assetChildren(o: THREE.Object3D): THREE.Object3D[] {
    const h = this.holder(o);
    const out: THREE.Object3D[] = [];
    for (const c of h.children) {
      const m = c.userData.marine as MarineData | undefined;
      if (m?.role === 'asset' || m?.role === 'instance') out.push(c);
      else if (c.userData.chunk) out.push(...this.assetChildren(c));
    }
    return out;
  }

  private partsOf(o: THREE.Object3D, role: 'geom' | 'proxy'): THREE.Object3D[] {
    return this.holder(o).children.filter((c) => (c.userData.marine as MarineData | undefined)?.role === role);
  }

  meta(o: THREE.Object3D): MarineData { return o.userData.marine as MarineData; }

  isSystem(o: THREE.Object3D) { return this.systemSlots.has(this.meta(o).id!); }

  levelName(depth: number) { return LEVEL_NAMES[Math.min(depth, LEVEL_NAMES.length - 1)]; }

  /** Depths that have any non-zero explode distance: these get a slider. */
  explodeLevels(): number[] {
    const ds = new Set<number>();
    this.shipRoot.traverse((o) => {
      const m = o.userData.marine as MarineData | undefined;
      if (m?.role === 'asset' && (m.explodeDist ?? 0) > 0 && o.userData.depth !== undefined) ds.add(o.userData.depth);
    });
    return [...ds].sort();
  }

  // ------------------------------------------------------------ explode / collapse
  setExplode(depth: number, v: number) { this.explode[depth] = v; this.applyExplode(); this.emit(); }

  applyExplode() {
    this.shipRoot.traverse((o) => {
      const m = o.userData.marine as MarineData | undefined;
      if (!m || m.role !== 'asset' || !o.userData.basePos || o.userData.isChunkRoot) return;
      const s = this.explode[o.userData.depth as number] ?? 0;
      const d = m.explodeDir ?? [0, 0, 0];
      const k = (m.explodeDist ?? 0) * s;
      o.position.copy(o.userData.basePos as THREE.Vector3).add(new THREE.Vector3(d[0] * k, d[1] * k, d[2] * k));
    });
    this.requestRender();
  }

  toggleExpanded(id: string) {
    const o = this.byId.get(id);
    if (!o) return;
    if (this.expanded.has(id)) this.expanded.delete(id);
    else {
      this.expanded.add(id);
      // expanding below equipment level needs the full-detail chunk
      const sys = this.systemOf(o);
      if (sys && this.loadedLod.get(sys) !== 'L2' && !this.isSystem(o)) {
        void this.loadSystem(sys, 'L2');
      }
    }
    this.applyVisibility();
    this.emit();
  }

  setAllExpanded(open: boolean) {
    this.shipRoot.traverse((o) => {
      const m = o.userData.marine as MarineData | undefined;
      if (m?.role === 'asset' && m.id && this.collapsible(o)) {
        if (open) this.expanded.add(m.id); else if (!o.userData.skeleton) this.expanded.delete(m.id);
      }
    });
    if (open) for (const id of this.systemSlots.keys()) void this.loadSystem(id, 'L2');
    this.applyVisibility();
    this.emit();
  }

  systemOf(o: THREE.Object3D): string | null {
    let p: THREE.Object3D | null = o;
    while (p) { const id = (p.userData.marine as MarineData | undefined)?.id; if (id && this.systemSlots.has(id)) return id; p = p.parent; }
    return null;
  }

  /** Collapsed: proxy shown, children hidden. Expanded: proxy hidden, own geometry + children shown. */
  applyVisibility() {
    const iso = this.isolatedId ? this.byId.get(this.isolatedId) ?? null : null;
    const onPath = new Set<THREE.Object3D>();
    if (iso) for (let p: THREE.Object3D | null = iso; p; p = p.parent) onPath.add(p);
    const visit = (o: THREE.Object3D, parentHidden: boolean, insideIso: boolean) => {
      const m = this.meta(o);
      const kids = this.assetChildren(o);
      const inside = insideIso || o === iso;
      const isoHidden = iso !== null && !inside && !onPath.has(o) && !onPath.has(this.holder(o));
      const hidden = parentHidden || o.userData.userHidden === true || isoHidden;
      const ancestorOnly = iso !== null && !inside;
      const geoms = this.partsOf(o, 'geom'), proxies = this.partsOf(o, 'proxy');
      // collapsible = has a proxy or children; an expanded node whose detail is
      // not loaded yet (L0 chunk) keeps showing its proxy until the chunk arrives
      const open = (kids.length === 0 && proxies.length === 0) || this.expanded.has(m.id!) || (ancestorOnly && onPath.has(o));
      const nothingElse = kids.length === 0 && geoms.length === 0;
      for (const g of geoms) g.visible = !hidden && open && !ancestorOnly;
      for (const p of proxies) p.visible = !hidden && (!open || nothingElse) && !ancestorOnly;
      for (const k of kids) {
        k.visible = true;
        if ((k.userData.marine as MarineData).role === 'instance') {
          k.visible = !hidden && open && (inside || !iso || k === iso);
        } else visit(k, hidden || !open, inside);
      }
    };
    visit(this.byId.get(this.manifest.vessel.id)!, false, false);
    this.requestRender();
  }

  setHidden(id: string, hidden: boolean) {
    const o = this.byId.get(id);
    if (!o) return;
    o.userData.userHidden = hidden;
    this.applyVisibility();
    this.emit();
  }

  isolatedId: string | null = null;

  /** Show only one asset (and keep its ancestors' frames), or null to show everything. */
  isolate(id: string | null) {
    this.isolatedId = id;
    this.applyVisibility();
    this.fit(id ?? undefined);
    this.emit();
  }

  // ------------------------------------------------------------ picking & selection
  private downAt = new THREE.Vector2();
  private onPointerDown(e: PointerEvent) { this.downAt.set(e.clientX, e.clientY); }
  private onPointerUp(e: PointerEvent) {
    if (this.downAt.distanceTo(new THREE.Vector2(e.clientX, e.clientY)) > 4) return; // was a drag
    const hit = this.raycast(e);
    if (this.measuring) {
      if (hit) this.addMeasurePoint(hit.point);
      return;
    }
    this.select(hit ? this.resolvePick(hit.object) : null);
  }

  raycast(e: { clientX: number; clientY: number }) {
    const r = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    const rc = new THREE.Raycaster();
    rc.setFromCamera(ndc, this.camera);
    const hits = rc.intersectObject(this.shipRoot, true).filter((h) => {
      for (let p: THREE.Object3D | null = h.object; p; p = p.parent) if (!p.visible) return false;
      return this.clip.distanceToPoint(h.point) >= 0;
    });
    return hits[0] ?? null;
  }

  /** Walk up from a mesh to the asset at the chosen selection level. */
  resolvePick(obj: THREE.Object3D): THREE.Object3D | null {
    let o: THREE.Object3D | null = obj;
    let owner: THREE.Object3D | null = null;
    while (o) {
      const m = o.userData.marine as MarineData | undefined;
      if (m && (m.role === 'asset' || m.role === 'instance')) { owner = o; break; }
      if (m && (m.role === 'geom' || m.role === 'proxy') && m.of) { owner = this.byId.get(m.of) ?? null; if (owner) break; }
      o = o.parent;
    }
    if (!owner) return null;
    const target = { part: 99, component: 4, equipment: 3, system: 2 }[this.pickLevel];
    let cur: THREE.Object3D = owner;
    while ((cur.userData.depth ?? 0) > target) {
      let p = cur.parent;
      while (p && !((p.userData.marine as MarineData | undefined)?.role === 'asset')) p = p.parent;
      if (!p) break;
      cur = p.userData.isChunkRoot ? this.byId.get((p.userData.marine as MarineData).id!)! : p;
    }
    return cur;
  }

  select(o: THREE.Object3D | null) {
    if (this.selected) this.paintHighlight(this.selected, false);
    this.selected = o;
    if (o) this.paintHighlight(o, true);
    this.requestRender();
    this.emit();
  }

  selectId(id: string) { this.select(this.byId.get(id) ?? null); }

  private paintHighlight(o: THREE.Object3D, on: boolean) {
    this.holder(o).traverse((x) => {
      if (!(x instanceof THREE.Mesh)) return;
      if (!on) { x.material = x.userData.shownMaterial ?? x.userData.origMaterial; return; }
      const base = x.material as THREE.Material;
      x.userData.shownMaterial = base;
      let h = this.highlightMats.get(base);
      if (!h) {
        const c = (base as THREE.MeshStandardMaterial).clone();
        c.emissive = new THREE.Color(0x0e7c86);
        c.emissiveIntensity = 0.55;
        this.highlightMats.set(base, c);
        h = c;
      }
      x.material = h;
    });
  }

  // ------------------------------------------------------------ colour modes
  setColorMode(mode: ColorMode) { this.colorMode = mode; void this.refreshColors(); }

  private async refreshColors() {
    if (this.colorMode === 'status' && this.opts.apiBase) await this.fetchStatuses();
    this.applyColors();
    this.emit();
  }

  private flat(key: string, hex: number): THREE.MeshStandardMaterial {
    let m = this.overrideMats.get(key);
    if (!m) {
      m = new THREE.MeshStandardMaterial({ color: hex, roughness: 0.6, metalness: 0.1, clippingPlanes: [this.clip] });
      this.overrideMats.set(key, m);
    }
    return m;
  }

  applyColors() {
    const hues = [0x2f6f9f, 0xb5651d, 0x4f8a3c, 0x8a4f9e, 0xa83c3c, 0x3c8a8a, 0x8a7a3c];
    const sel = this.selected;
    if (sel) this.paintHighlight(sel, false);
    this.shipRoot.traverse((x) => {
      if (!(x instanceof THREE.Mesh)) return;
      let mat: THREE.Material | THREE.Material[] = x.userData.origMaterial;
      if (this.colorMode !== 'material') {
        // find owner asset and its equipment ancestor
        let owner: THREE.Object3D | null = x, eq: THREE.Object3D | null = null, ownerId: string | null = null;
        for (let p: THREE.Object3D | null = x; p; p = p.parent) {
          const m = p.userData.marine as MarineData | undefined;
          if (!ownerId && m && (m.role === 'asset' || m.role === 'instance')) { ownerId = m.id!; owner = p; }
          if (!ownerId && m?.of) ownerId = m.of;
          if (m?.role === 'asset' && p.userData.depth === 3) eq = p;
        }
        void owner;
        if (this.colorMode === 'equipment') {
          const eqId = eq ? (eq.userData.marine as MarineData).id! : 'none';
          const idx = [...this.byId.keys()].filter((k) => this.byId.get(k)!.userData.depth === 3).indexOf(eqId);
          mat = this.flat(`eq:${idx}`, hues[(idx + hues.length) % hues.length]);
        } else {
          const st = ownerId ? this.status.get(ownerId)?.condition : undefined;
          const hex = st === 'poor' ? 0xc0392b : st === 'fair' ? 0xc98a00 : st === 'good' ? 0x2f8f46 : 0xb6c0c8;
          mat = this.flat(`st:${st ?? 'none'}`, hex);
        }
      }
      x.material = mat;
      x.userData.shownMaterial = undefined;
    });
    if (sel) this.paintHighlight(sel, true);
    this.requestRender();
  }

  async fetchStatuses() {
    const ids: string[] = [];
    this.shipRoot.traverse((o) => {
      const m = o.userData.marine as MarineData | undefined;
      if (m?.role === 'asset' && m.id && !this.status.has(m.id)) ids.push(m.id);
    });
    const queue = [...ids];
    const work = async () => {
      while (queue.length) {
        const id = queue.shift()!;
        try {
          const r = await fetch(this.api(`assets/${id}/status`));
          if (r.ok) this.status.set(id, await r.json());
        } catch { /* offline */ }
      }
    };
    await Promise.all(Array.from({ length: 6 }, work));
  }

  api(path: string): string {
    const base = this.opts.apiBase ?? '/';
    return new URL(path, new URL(base.endsWith('/') ? base : base + '/', location.href)).toString();
  }

  // ------------------------------------------------------------ section & measure
  setSection(x: number | null) {
    this.clip.constant = x === null ? 1e6 : x;
    this.requestRender();
  }

  setMeasuring(on: boolean) {
    this.measuring = on;
    this.measurePts = [];
    this.measureGroup.clear();
    this.lastMeasure = null;
    this.requestRender();
    this.emit();
  }

  private addMeasurePoint(p: THREE.Vector3) {
    if (this.measurePts.length === 2) { this.measurePts = []; this.measureGroup.clear(); }
    this.measurePts.push(p.clone());
    const dot = new THREE.Mesh(new THREE.SphereGeometry(0.06, 12, 8), new THREE.MeshBasicMaterial({ color: 0x0e7c86, depthTest: false }));
    dot.position.copy(p);
    dot.renderOrder = 10;
    this.measureGroup.add(dot);
    if (this.measurePts.length === 2) {
      const g = new THREE.BufferGeometry().setFromPoints(this.measurePts);
      const line = new THREE.Line(g, new THREE.LineBasicMaterial({ color: 0x0e7c86, depthTest: false }));
      line.renderOrder = 10;
      this.measureGroup.add(line);
      this.lastMeasure = this.measurePts[0].distanceTo(this.measurePts[1]);
    }
    this.requestRender();
    this.emit();
  }

  // ------------------------------------------------------------ camera
  fit(id?: string) {
    const target = id ? this.byId.get(id) : this.shipRoot;
    if (!target) return;
    const box = new THREE.Box3();
    target.updateWorldMatrix(true, true);
    target.traverseVisible((o) => { if (o instanceof THREE.Mesh) box.expandByObject(o); });
    if (box.isEmpty()) return;
    const c = box.getCenter(new THREE.Vector3());
    const r = box.getSize(new THREE.Vector3()).length() / 2;
    // fit the bounding sphere in the narrower of the two fields of view
    const vfov = (this.camera.fov * Math.PI) / 180;
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * this.camera.aspect);
    const dist = (r / Math.sin(Math.min(vfov, hfov) / 2)) * 1.05;
    const dir = new THREE.Vector3(0.55, 0.42, 0.72).normalize();
    this.camera.position.copy(c).addScaledVector(dir, dist);
    this.camera.near = Math.max(dist / 500, 0.01);
    this.camera.far = dist * 20;
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(c);
    this.controls.update();
    this.requestRender();
  }

  /** Count triangles currently drawn (for the stats line). */
  drawnTriangles(): number {
    let n = 0;
    this.shipRoot.traverseVisible((o) => {
      if (o instanceof THREE.Mesh) {
        const g = o.geometry as THREE.BufferGeometry;
        n += (g.index ? g.index.count : g.attributes.position.count) / 3;
      }
    });
    return Math.round(n);
  }

  shipBoundsX(): [number, number] {
    const b = new THREE.Box3().setFromObject(this.shipRoot);
    return [b.min.x, b.max.x];
  }
}
