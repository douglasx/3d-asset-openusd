// Build a single self-contained HTML viewer (JS, CSS, manifest and GLB chunks
// inlined) for sharing without a server: node scripts/single-file.mjs <vessel> <out.html>
// Run after `npm run build`.
import fs from 'node:fs';
import path from 'node:path';
const [vessel = 'demo_osv', out = 'dist/viewer-single.html'] = process.argv.slice(2);
const dist = path.resolve('dist');
const html = fs.readFileSync(path.join(dist, 'index.html'), 'utf8');
const assets = fs.readdirSync(path.join(dist, 'assets'));
const js = fs.readFileSync(path.join(dist, 'assets', assets.find((f) => f.endsWith('.js'))), 'utf8').replaceAll('</script', '<\\/script');
const css = fs.readFileSync(path.join(dist, 'assets', assets.find((f) => f.endsWith('.css'))), 'utf8');
const dataDir = path.join(dist, 'data', vessel);
const manifest = JSON.parse(fs.readFileSync(path.join(dataDir, 'manifest.json'), 'utf8'));
const chunks = Object.fromEntries(manifest.chunks.map((c) => [c.url, fs.readFileSync(path.join(dataDir, c.url)).toString('base64')]));
const embed = `<script>window.__MARINE_EMBED__=${JSON.stringify({ manifest, chunks })};</script>`;
const page = html
  // function replacers: the minified bundle contains `$&`-style sequences
  .replace(/<link rel="stylesheet"[^>]*assets[^>]*>/, () => `<style>${css}</style>`)
  .replace(/<script type="module"[^>]*src="[^"]*"[^>]*><\/script>/, () => '')
  .replace('</body>', () => `${embed}<script type="module">${js}</script></body>`);
fs.writeFileSync(out, page);
console.log(`wrote ${out} (${(page.length / 1e6).toFixed(2)} MB)`);
