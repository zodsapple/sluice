import { mkdirSync, copyFileSync, cpSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname } from 'node:path';
process.chdir(dirname(fileURLToPath(import.meta.url)));
mkdirSync('dist', { recursive: true });
for (const name of ['index.html', 'quickstart.html', 'style.css', 'app.js', 'worker.js', 'runtime.py']) copyFileSync(name, `dist/${name}`);
cpSync('assets', 'dist/assets', { recursive: true });
cpSync('vendor', 'dist/python', { recursive: true });
console.log('Static playground built in dist/');
