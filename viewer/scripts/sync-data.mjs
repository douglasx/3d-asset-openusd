// Copy build outputs (manifest + GLB chunks) into public/data so the viewer
// can run as a static site with no API. Skips raw/unoptimised chunks.
import fs from 'node:fs';
import path from 'node:path';
const src = path.resolve('../build/out/vessels');
const dst = path.resolve('public/data');
if (!fs.existsSync(src)) { console.warn('no build output yet: run `make build` first'); process.exit(0); }
fs.rmSync(dst, { recursive: true, force: true });
for (const vessel of fs.readdirSync(src)) {
  const from = path.join(src, vessel);
  if (!fs.statSync(from).isDirectory()) continue;
  fs.mkdirSync(path.join(dst, vessel), { recursive: true });
  for (const f of fs.readdirSync(from)) {
    if (f.endsWith('.glb') || f === 'manifest.json') fs.copyFileSync(path.join(from, f), path.join(dst, vessel, f));
  }
}
console.log('synced viewer data from build/out/vessels');
