// Bundles the card UI into a single Anki media file: dist/_knightly.js
// (the leading underscore keeps Anki's "Check Media" from deleting it).
import * as esbuild from 'esbuild';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';

const watch = process.argv.includes('--watch');

const options = {
  entryPoints: ['src/main.ts'],
  bundle: true,
  format: 'iife',
  globalName: 'KnightlyReview',
  target: ['es2018'],
  loader: { '.css': 'text' },
  minify: !watch,
  sourcemap: watch ? 'inline' : false,
  legalComments: 'eof',
  outfile: 'dist/_knightly.js',
  logLevel: 'info',
};

/** Inline the shared loader into the card templates → dist/templates/. */
function writeTemplates() {
  mkdirSync('dist/templates', { recursive: true });
  const loader = readFileSync('templates/loader.html', 'utf8').trim();
  for (const name of ['front.html', 'back.html']) {
    const src = readFileSync('templates/' + name, 'utf8').replace('<!--KR_LOADER-->', loader);
    writeFileSync('dist/templates/' + name, src);
  }
  writeFileSync('dist/templates/style.css', readFileSync('templates/style.css', 'utf8'));
}

writeTemplates();
if (watch) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
} else {
  await esbuild.build(options);
}
