// Loads e2e/oracle.ts (the card's own PGN + grading code) bundled for Node.
import * as esbuild from 'esbuild';
import { pathToFileURL } from 'node:url';
import { E2E_DIR } from './common.mjs';

let cached;

/** @returns {Promise<{ solve: (pgn: string, n: number, accept?: number[]) => any }>} */
export async function oracle() {
  if (cached) return cached;
  const outfile = E2E_DIR + '.cache/oracle.mjs';
  await esbuild.build({
    entryPoints: [E2E_DIR + 'oracle.ts'],
    bundle: true,
    format: 'esm',
    platform: 'node',
    outfile,
    logLevel: 'silent',
  });
  cached = await import(pathToFileURL(outfile).href + '?t=' + Date.now());
  return cached;
}
