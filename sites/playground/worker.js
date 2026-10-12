import { loadPyodide } from './python/pyodide.mjs';
const BASE = new URL('python/', self.location.href).href;
let python;
async function initialize() {
  if (python) return;
  postMessage({ progress: 'Loading browser Python… First start can take a little while.' });
  const runtime = await loadPyodide({ indexURL: BASE });
  postMessage({ progress: 'Loading pandas, validation and Excel support…' });
  await runtime.loadPackage(['pandas', 'pydantic', 'pyyaml', 'openpyxl']);
  const bundle = new Uint8Array(await (await fetch('assets/sluice.zip')).arrayBuffer());
  runtime.FS.writeFile('/sluice.zip', bundle);
  runtime.runPython("import sys, zipfile\nzipfile.ZipFile('/sluice.zip').extractall('/engine')\nsys.path.insert(0, '/engine')");
  await runtime.runPythonAsync(await (await fetch('runtime.py')).text());
  python = runtime;
  postMessage({ progress: 'Python ready. Files are processed on this device.' });
}
self.onmessage = async ({ data }) => {
  try {
    await initialize();
    python.runPython('reset_session()');
    for (const file of data.files) python.FS.writeFile(`/session/${file.name}`, new Uint8Array(file.bytes));
    python.globals.set('request_json', JSON.stringify(data.request));
    const result = JSON.parse(await python.runPythonAsync('execute_browser(request_json)'));
    if (result.error) {
      postMessage({ id: data.id, error: result.error });
      return;
    }
    if (result.output) {
      const bytes = python.FS.readFile(result.output);
      result.download = { name: result.output.split('/').pop(), bytes: bytes.buffer };
      postMessage({ id: data.id, result }, [bytes.buffer]);
    } else postMessage({ id: data.id, result });
  } catch (error) {
    postMessage({ id: data.id, error: String(error.message || error) });
  }
};
