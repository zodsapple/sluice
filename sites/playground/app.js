const $ = id => document.getElementById(id);
let inputFile, templateFile, schemaText = '', worker, busy = false, pending, requestId = 0;
let evidence, validated = false, activeTab = 'output', prompts = [], downloadUrls = [];
let generatedSpec = false, exampleLoaded = false;
const MAX_FILE = 5 * 1024 * 1024;

function status(text, kind = '') {
  $('status-text').textContent = text; $('status').className = `status ${kind}`;
  $('upload-note').textContent = text; $('upload-note').className = `upload-note ${kind}`;
}
function updateButtons() {
  $('preview').disabled = busy || !inputFile || !$('spec').value.trim();
  $('convert').disabled = busy || !validated || !$('reviewed').checked;
  $('draft').disabled = busy || !inputFile || !(schemaText || templateFile);
  $('download-spec').disabled = busy || !$('spec').value.trim();
  for (const element of document.querySelectorAll('input[type=file], .demo, #spec, #sheet-names, #target-role, #target-layout, #reviewed')) element.disabled = busy;
  $('cancel').hidden = !busy;
}
function invalidate() {
  clearDownloads();
  validated = false; evidence = null; $('reviewed').checked = false;
  $('results').hidden = true; $('downloads').replaceChildren(); updateButtons();
}
async function acceptFile(kind, file) {
  if (!file) return;
  if (file.size > MAX_FILE) throw new Error('Choose a file of 5 MB or less. Use the local CLI for larger files.');
  const extension = file.name.split('.').pop().toLowerCase();
  if (kind === 'schema' && extension !== 'json') throw new Error('Choose a JSON target schema.');
  if (kind !== 'schema' && !['csv', 'xlsx'].includes(extension)) throw new Error('Choose a CSV or XLSX file.');
  if (exampleLoaded) {
    inputFile = templateFile = undefined; schemaText = ''; exampleLoaded = false;
    $('input-label').textContent = 'Choose or drop an input file';
    $('template-label').textContent = 'Choose an expected result or template';
    $('schema-label').textContent = 'Choose target schema'; $('sheet-names').value = '';
    $('target-role').value = 'expected';
    for (const demo of document.querySelectorAll('.demo')) demo.classList.remove('selected');
  }
  if (generatedSpec) { $('spec').value = ''; generatedSpec = false; }
  $('draft-review').hidden = true; prompts = []; invalidate();
  if (kind === 'input') { inputFile = file; $('input-label').textContent = file.name; }
  if (kind === 'template') { templateFile = file; $('template-label').textContent = file.name; }
  if (kind === 'schema') {
    schemaText = ''; $('schema-label').textContent = 'Choose target schema'; updateButtons();
    const text = await file.text(); JSON.parse(text); schemaText = text; $('schema-label').textContent = file.name;
  }
  updateButtons();
  if (inputFile && (templateFile || schemaText) && !$('spec').value.trim()) await execute('draft');
  else status(!inputFile ? 'Target selected. Choose the input file to generate rules.' : !$('spec').value.trim() ? 'Input selected. Add an expected result, a template or a JSON schema to generate rules.' : 'File selected. Review the existing YAML, or draft new rules from the target.');
}
for (const kind of ['input', 'template', 'schema']) {
  $(`${kind}-file`).addEventListener('change', async event => {
    try { await acceptFile(kind, event.target.files[0]); } catch (error) { status(error.message, 'bad'); }
  });
  const zone = document.querySelector(`[data-upload="${kind}"]`);
  zone.addEventListener('dragover', event => { event.preventDefault(); zone.classList.add('drag'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag'));
  zone.addEventListener('drop', async event => {
    event.preventDefault(); zone.classList.remove('drag'); if (busy) return;
    try { await acceptFile(kind, event.dataTransfer.files[0]); } catch (error) { status(error.message, 'bad'); }
  });
}
$('spec-file').addEventListener('change', async event => {
  try { const file = event.target.files[0]; if (!file) return; if (file.size > 256 * 1024) throw new Error('Spec is too large.'); $('spec').value = await file.text(); generatedSpec = false; exampleLoaded = false; invalidate(); status('Spec loaded. Preview & validate to check your files.'); }
  catch (error) { status(error.message, 'bad'); }
});
$('spec').addEventListener('input', () => { generatedSpec = false; invalidate(); });
$('sheet-names').addEventListener('input', invalidate);
for (const id of ['target-role', 'target-layout']) $(id).addEventListener('change', () => { invalidate(); status('Target options changed. Draft again if the layout changed, then preview & validate.'); });
$('reviewed').addEventListener('change', updateButtons);

async function asset(name) { const response = await fetch(`assets/${name}`); if (!response.ok) throw new Error(`Could not load ${name}`); return response; }
for (const button of document.querySelectorAll('.demo')) button.addEventListener('click', async () => {
  busy = true; updateButtons(); status('Loading the example…', 'busy');
  try {
    const transport = button.dataset.demo === 'transport';
    const [input, template, spec, schema] = await Promise.all([
      asset(transport ? 'transport.xlsx' : 'input.csv'), asset(transport ? 'transport-template.xlsx' : 'template.csv'),
      asset(transport ? 'transport.yaml' : 'spec.yaml'), asset('schema.json')]);
    let blob = await input.blob();
    if (button.dataset.demo === 'failure') blob = new Blob([(await blob.text()).replace('100.125', '100.129')]);
    inputFile = new File([blob], transport ? 'freight-invoice.xlsx' : 'deliveries.csv');
    templateFile = new File([await template.blob()], transport ? 'transport-template.xlsx' : 'template.csv');
    schemaText = transport ? '' : await schema.text();
    $('input-label').textContent = inputFile.name; $('template-label').textContent = templateFile.name;
    $('schema-label').textContent = schemaText ? 'Included delivery schema' : 'Choose target schema';
    $('spec').value = await spec.text(); $('sheet-names').value = '';
    generatedSpec = exampleLoaded = true; $('target-role').value = 'template'; $('target-layout').value = 'auto';
    $('draft-review').hidden = true; prompts = [];
    for (const demo of document.querySelectorAll('.demo')) demo.classList.toggle('selected', demo === button);
    invalidate(); status('Example ready. Inspect the YAML, then preview & validate.');
  } catch (error) { status(error.message, 'bad'); }
  finally { busy = false; updateButtons(); }
});

function getWorker() {
  if (worker) return worker;
  worker = new Worker('worker.js', { type: 'module' });
  worker.onmessage = ({ data }) => {
    if (data.progress) { status(data.progress, 'busy'); return; }
    if (!pending || data.id !== pending.id) return;
    const { resolve, reject } = pending; pending = null;
    if (data.error) reject(new Error(data.error)); else resolve(data.result);
  };
  worker.onerror = error => {
    worker.terminate(); worker = null;
    if (pending) { pending.reject(new Error(error.message || 'Browser Python could not start.')); pending = null; }
  };
  return worker;
}
async function execute(action) {
  busy = true; validated = false; updateButtons(); status('Starting browser Python…', 'busy');
  try {
    const inputExt = inputFile.name.split('.').pop().toLowerCase();
    const files = [{ name: `input.${inputExt}`, bytes: await inputFile.arrayBuffer() }];
    let templateName;
    if (templateFile) {
      templateName = `template.${templateFile.name.split('.').pop().toLowerCase()}`;
      files.push({ name: templateName, bytes: await templateFile.arrayBuffer() });
    }
    const request = { action, input: files[0].name, template: templateName, spec: $('spec').value,
                      schema: schemaText, targetRole: $('target-role').value, targetLayout: $('target-layout').value,
                      sheets: $('sheet-names').value.split('\n').map(s => s.trim()).filter(Boolean) };
    const id = ++requestId;
    const result = await new Promise((resolve, reject) => {
      pending = { id, resolve, reject };
      getWorker().postMessage({ id, files, request }, files.map(f => f.bytes));
    });
    if (action === 'draft') {
      $('spec').value = result.spec; generatedSpec = true; invalidate(); prompts = result.prompts;
      $('draft-notes').textContent = result.review;
      $('draft-review').hidden = false; $('draft-review').open = true;
      $('download-prompts').hidden = !prompts.length;
      status(`Draft ready: ${result.questions.length} open question(s). Review the YAML and draft notes, then Preview & validate.`, result.questions.length ? '' : 'good');
    } else {
      evidence = result; validated = result.ok && result.report.controls.some(c => c.on_fail === 'halt' && !c.kind.startsWith('rule:'));
      if (action === 'convert') result.report.browser_review = { confirmed: true, at: new Date().toISOString() };
      renderEvidence(result, action);
      status(result.ok ? (action === 'convert' ? 'Conversion complete. Download the result and its evidence.' : validated ? 'Declared checks passed. Review the mapping and scope before converting.' : 'No halting reconciliation controls declared. Add them before converting.') : 'Validation failed. No new output is available.', result.ok ? 'good' : 'bad');
    }
  } catch (error) {
    validated = false; $('results').hidden = true; status(error.message, 'bad');
  } finally { busy = false; updateButtons(); }
}
$('preview').addEventListener('click', () => execute('preview'));
$('convert').addEventListener('click', () => execute('convert'));
$('draft').addEventListener('click', () => execute('draft'));
$('cancel').addEventListener('click', () => {
  worker?.terminate(); worker = null;
  if (pending) { pending.reject(new Error('Stopped. The next run will start a fresh Python runtime.')); pending = null; }
});

function download(name, bytes, type = 'application/octet-stream') {
  const url = URL.createObjectURL(new Blob([bytes], { type }));
  const a = document.createElement('a'); a.href = url; a.download = name;
  a.hidden = true; document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}
function clearDownloads() { downloadUrls.forEach(url => URL.revokeObjectURL(url)); downloadUrls = []; }
$('download-spec').addEventListener('click', () => download('spec.yaml', $('spec').value, 'text/yaml'));
$('download-prompts').addEventListener('click', () => download('chat-prompts.txt', prompts.join('\n\n----------\n\n'), 'text/plain'));
function node(tag, text, className) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el; }
function table(columns, rows) {
  const t = node('table'), head = node('thead'), tr = node('tr'), body = node('tbody');
  for (const col of columns) tr.append(node('th', col)); head.append(tr); t.append(head);
  for (const row of rows) { const r = node('tr'); for (const value of row) { const cell = node('td', value ?? ''); cell.title = String(value ?? ''); r.append(cell); } body.append(r); }
  t.append(body); return t;
}
function renderEvidence(result, action) {
  const report = result.report;
  $('results').hidden = false; $('result-title').textContent = result.ok ? (action === 'convert' ? 'Converted. Checks passed.' : 'Ready for your review.') : 'Stopped by validation.';
  $('metrics').replaceChildren();
  for (const [label, value, kind] of [['Source rows', report.rows_in], ['Output rows', report.rows_out], ['Removed rows', report.dropped_rows], ['Declared checks', result.ok ? 'PASS' : 'FAIL', result.ok ? 'pass' : 'fail']]) {
    const metric = node('div', undefined, `metric ${kind || ''}`); metric.append(node('span', label), node('strong', value)); $('metrics').append(metric);
  }
  $('diagnosis').hidden = !report.diagnosis;
  $('diagnosis').textContent = report.diagnosis ? `${report.diagnosis.code}: ${report.diagnosis.message}` : '';
  $('controls').replaceChildren();
  for (const c of report.controls) {
    const row = node('div', undefined, `control ${c.passed ? '' : 'fail'}`);
    row.append(node('span', c.passed ? 'PASS' : c.on_fail === 'halt' ? 'FAIL' : 'WARN', 'pill'), node('strong', c.name),
               node('code', c.kind === 'expected_rows' ? `expected ${c.expected} rows · output ${c.output} rows` : `source ${c.source} − removed ${c.dropped} → output ${c.output}${c.expected == null ? '' : ` · declared ${c.expected}`}`));
    if (c.reasons.length) row.append(node('small', c.reasons.join('; '))); $('controls').append(row);
  }
  if (!report.controls.length) $('controls').append(node('p', 'No controls declared. Review and add reconciliation checks to the spec.', 'empty'));
  clearDownloads(); $('downloads').replaceChildren();
  const addDownload = (label, name, bytes, type = 'application/octet-stream') => {
    const a = node('a', label, 'text-button'); a.href = URL.createObjectURL(new Blob([bytes], { type }));
    a.download = name; downloadUrls.push(a.href); $('downloads').append(a);
  };
  if (result.download) addDownload('Download result ↓', result.download.name, result.download.bytes);
  addDownload('Report JSON ↓', 'run-report.json', JSON.stringify(report, null, 2), 'application/json');
  if (result.feedback) addDownload('Feedback ↓', 'feedback.md', result.feedback, 'text/markdown');
  activeTab = 'output'; syncTabs(); renderDetail(); $('results').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
function renderDetail() {
  $('detail').replaceChildren(); if (!evidence) return;
  const r = evidence.report;
  if (activeTab === 'output') {
    const p = evidence.outputPreview;
    $('detail').append(p ? table(p.columns, p.rows) : node('p', 'The run failed before an output preview was available.', 'empty'));
  } else if (activeTab === 'input') {
    for (const [sheet, p] of Object.entries(evidence.inputPreview)) {
      $('detail').append(node('h3', `${sheet} · ${p.rows.toLocaleString()} raw rows`));
      $('detail').append(table(p.cells[0]?.map((_, i) => `Column ${i + 1}`) || [], p.cells));
    }
  } else if (activeTab === 'issues') {
    $('detail').append(r.issues.length ? table(['Severity', 'Reason', 'Source sheet', 'Row', 'Field', 'Message'], r.issues.map(i => [i.severity, i.reason_code, i.source_sheet, i.source_row, i.field_name, i.message])) : node('p', 'No issues reported.', 'empty'));
  } else $('detail').append(table(['Step', 'Rows in', 'Rows out', 'Removed rows', 'Removed value'], r.traces.map(t => [t.step, t.rows_in, t.rows_out, t.dropped_rows, t.dropped_amount])));
}
function syncTabs() { for (const tab of document.querySelectorAll('.tab')) { const active = tab.dataset.tab === activeTab; tab.classList.toggle('active', active); tab.setAttribute('aria-selected', String(active)); } }
for (const tab of document.querySelectorAll('.tab')) tab.addEventListener('click', () => { activeTab = tab.dataset.tab; syncTabs(); renderDetail(); });
asset('version.json').then(r => r.json()).then(v => { $('version').textContent = v.version; $('version').title = `Python bundle SHA-256: ${v.sha256}`; }).catch(() => {});
updateButtons();
