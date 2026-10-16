// GLB optimisation step: dedup + weld + meshopt compression (with quantization).
// Usage: node optimize.mjs <out_dir> <in1.glb> [<in2.glb> ...]
// Prints a JSON map {input: output} on stdout.
//
// Deliberately NOT used: instance() and join()/flatten(), which would merge
// nodes and destroy the one-node-per-ID contract the viewer relies on.
import { NodeIO, Logger } from '@gltf-transform/core';
import { ALL_EXTENSIONS } from '@gltf-transform/extensions';
import { dedup, weld, prune, meshopt } from '@gltf-transform/functions';
import { MeshoptEncoder, MeshoptDecoder } from 'meshoptimizer';
import path from 'node:path';
import fs from 'node:fs';

const [outDir, ...inputs] = process.argv.slice(2);
if (!outDir || inputs.length === 0) {
  console.error('usage: node optimize.mjs <out_dir> <in.glb>...');
  process.exit(2);
}
await MeshoptEncoder.ready;
await MeshoptDecoder.ready;
const io = new NodeIO()
  .registerExtensions(ALL_EXTENSIONS)
  .registerDependencies({ 'meshopt.encoder': MeshoptEncoder, 'meshopt.decoder': MeshoptDecoder });

fs.mkdirSync(outDir, { recursive: true });
const result = {};
for (const input of inputs) {
  const doc = await io.read(input);
  doc.setLogger(new Logger(Logger.Verbosity.WARN));
  await doc.transform(
    dedup(),
    weld(),
    prune({ keepLeaves: true, keepAttributes: true, keepExtras: true }),
    meshopt({ encoder: MeshoptEncoder, level: 'medium' }),
  );
  const out = path.join(outDir, path.basename(input));
  await io.write(out, doc);
  result[input] = out;
}
process.stdout.write(JSON.stringify(result));
