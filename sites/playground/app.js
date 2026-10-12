import { pairKey, remember } from './session.mjs';
const $ = id => document.getElementById(id);
let inputFile, templateFile, schemaText = '', worker, busy = false, pending, requestId = 0;
let evidence, validated = false, activeTab = 'output', prompts = [], downloadUrls = [];
let workspace, currentKey = '', configDirty = false, yamlDirty = false, exampleLoaded = false;
let configUrl;
const MAX_FILE = 5 * 1024 * 1024, configs = new Map(), manualDrafts = new Map();
try { for (const [key, config] of JSON.parse(sessionStorage.getItem('sluice-configs-v1') || '[]')) remember(configs, key, config); } catch {}

function status(text, kind = '') {
  $('status-text').textContent = text; $('status').className = `status ${kind}`;
  $('upload-note').textContent = text; $('upload-note').className = `upload-note ${kind}`;
}
function updateButtons() {
  $('preview').disabled = busy || !inputFile || !$('spec').value.trim() || configDirty || yamlDirty;
  $('convert').disabled = busy || !validated || !$('reviewed').checked || configDirty || yamlDirty;
  $('draft').disabled = busy || !inputFile || !(schemaText || templateFile);
  $('download-spec').disabled = busy || !$('spec').value.trim() || yamlDirty;
  $('download-config').disabled = busy || !workspace || configDirty || yamlDirty;
  $('download-config').setAttribute('aria-disabled', String($('download-config').disabled));
  $('apply-config').disabled = busy || !workspace || !configDirty;
  $('revert-config').disabled = busy || !workspace || !(configDirty || yamlDirty);
  $('apply-yaml').disabled = busy || !workspace || !yamlDirty;
  $('apply-reply').disabled = busy || !workspace || configDirty || yamlDirty || !$('reply').value.trim();
  for(const id of ['copy-repair','download-repair','paste-correction']) $(id).disabled=busy||configDirty||yamlDirty||!evidence||!$('repair-prompt').value;
  for (const el of document.querySelectorAll('input[type=file], .demo, #config, #spec, #reply, #sheet-names, #target-role, #target-layout, #reviewed, .manual-control')) el.disabled = busy || (el.classList.contains('manual-control') && (configDirty || yamlDirty));
  $('config-file').disabled = busy || !inputFile;
  $('cancel').hidden = !busy;
}
function invalidate() {
  clearDownloads(); validated = false; evidence = null; $('reviewed').checked = false;
  $('results').hidden = true; $('downloads').replaceChildren(); updateButtons();
  $('repair-feedback').hidden=true; $('repair-prompt').value=''; $('repair-sheet').replaceChildren(); $('repair-notice').textContent='';
}
function resetWorkspace() {
  if (configUrl) URL.revokeObjectURL(configUrl); configUrl = undefined; $('download-config').removeAttribute('href');
  workspace = undefined; currentKey = ''; configDirty = yamlDirty = false; manualDrafts.clear();
  $('config').value = ''; $('spec').value = ''; $('reply').value = ''; $('reply-notice').textContent = '';
  $('resolve').hidden = $('draft-review').hidden = true; prompts = []; invalidate();
}
function cacheConfig() {
  if (!workspace || !currentKey) return;
  remember(configs, currentKey, workspace.config);
  try { sessionStorage.setItem('sluice-configs-v1', JSON.stringify([...configs])); $('cache-note').textContent = 'Config cached for this file pair in this tab session. Validation and approval are never cached.'; }
  catch { $('cache-note').textContent = 'Config cached in memory. Session storage is unavailable; export JSON to keep a copy.'; }
}
function showWorkspace(result) {
  workspace = result; configDirty = yamlDirty = false; prompts = result.prompts;
  $('config').value = JSON.stringify(result.config, null, 2); $('spec').value = result.spec;
  if (configUrl) URL.revokeObjectURL(configUrl);
  configUrl = URL.createObjectURL(new Blob([JSON.stringify(result.config, null, 2)], {type:'application/json'}));
  $('download-config').href = configUrl; $('download-config').download = 'sluice-config.json';
  $('draft-notes').textContent = result.review; $('draft-review').hidden = false;
  $('reply-notice').textContent = result.notices.join('\n');
  renderQuestions(); renderPrompts(); invalidate(); cacheConfig();
}
async function draftOrRestore() {
  if (busy || !inputFile || !(templateFile || schemaText)) return;
  let key;
  busy = true; updateButtons(); status('Identifying the file pair…', 'busy');
  try { key = await pairKey(inputFile, templateFile, schemaText, {role: $('target-role').value, layout: $('target-layout').value, sheets: $('sheet-names').value}); }
  finally { busy = false; updateButtons(); }
  if (key !== currentKey) manualDrafts.clear(); currentKey = key;
  const cached = configs.get(key);
  await execute(cached ? 'load_config' : 'draft', cached ? {config: cached, restored: true} : {});
}
async function acceptFile(kind, file) {
  if (!file) return;
  if (file.size > MAX_FILE) throw new Error('Choose a file of 5 MB or less. Use the local CLI for larger files.');
  const ext = file.name.split('.').pop().toLowerCase();
  if (kind === 'schema' ? ext !== 'json' : !['csv','xlsx'].includes(ext)) throw new Error(kind === 'schema' ? 'Choose a JSON target schema.' : 'Choose a CSV or XLSX file.');
  if (exampleLoaded) {
    inputFile = templateFile = undefined; schemaText = ''; exampleLoaded = false;
    $('input-label').textContent = 'Choose or drop an input file'; $('template-label').textContent = 'Choose an expected result or template';
    $('schema-label').textContent = 'Choose target schema'; $('sheet-names').value = ''; $('target-role').value = 'expected'; $('target-layout').value = 'auto';
    for (const demo of document.querySelectorAll('.demo')) demo.classList.remove('selected');
  }
  resetWorkspace();
  if (kind === 'input') { inputFile = file; $('input-label').textContent = file.name; }
  if (kind === 'template') { templateFile = file; $('template-label').textContent = file.name; }
  if (kind === 'schema') {
    schemaText = ''; const text = await file.text(); JSON.parse(text); schemaText = text; $('schema-label').textContent = file.name;
  }
  updateButtons();
  if (inputFile && (templateFile || schemaText)) await draftOrRestore();
  else status(!inputFile ? 'Target selected. Choose the input file to generate a config.' : 'Input selected. Add an expected result, template or JSON schema.');
}
for (const kind of ['input','template','schema']) {
  $(`${kind}-file`).addEventListener('change', async event => { try { await acceptFile(kind, event.target.files[0]); } catch (e) { status(e.message,'bad'); } finally { event.target.value = ''; } });
  const zone = document.querySelector(`[data-upload="${kind}"]`);
  zone.addEventListener('dragover', event => { event.preventDefault(); zone.classList.add('drag'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag'));
  zone.addEventListener('drop', async event => { event.preventDefault(); zone.classList.remove('drag'); if (busy) return; try { await acceptFile(kind,event.dataTransfer.files[0]); } catch(e) { status(e.message,'bad'); } });
}
$('config-file').addEventListener('change', async event => {
  try { const f=event.target.files[0]; if(!f) return; if(f.size>1024*1024) throw new Error('Config exceeds 1 MB.'); const config=JSON.parse(await f.text()); await execute('load_config',{config}); }
  catch(e) { status(e.message,'bad'); } finally { event.target.value=''; }
});
$('spec-file').addEventListener('change', async event => {
  try { const f=event.target.files[0]; if(!f) return; if(f.size>256*1024) throw new Error('Spec is too large.'); $('spec').value=await f.text(); yamlDirty=Boolean(workspace); invalidate(); status(workspace ? 'YAML loaded. Apply edits to update the config.' : 'YAML loaded. Preview & validate your files.'); }
  catch(e) { status(e.message,'bad'); } finally { event.target.value=''; }
});
$('config').addEventListener('input', () => { configDirty=true; invalidate(); });
$('spec').addEventListener('input', () => { yamlDirty=Boolean(workspace); invalidate(); });
$('reply').addEventListener('input', updateButtons);
$('reviewed').addEventListener('change', updateButtons);
for (const id of ['target-role','target-layout','sheet-names']) $(id).addEventListener('change', () => { resetWorkspace(); status('Draft / restore a config for the new target options.'); });
$('apply-config').addEventListener('click', () => { try { execute('load_config',{config:JSON.parse($('config').value)}); } catch(e) { status(e.message,'bad'); } });
$('apply-yaml').addEventListener('click', () => execute('edit_yaml'));
$('revert-config').addEventListener('click', () => { if(workspace) showWorkspace(workspace); status('Edits reverted. Validate again before approving.'); });
$('apply-reply').addEventListener('click', () => execute('answer',{reply:$('reply').value}));

async function asset(name) { const r=await fetch(`assets/${name}`); if(!r.ok) throw new Error(`Could not load ${name}`); return r; }
for (const button of document.querySelectorAll('.demo')) button.addEventListener('click', async () => {
  resetWorkspace(); busy=true; updateButtons(); status('Loading the example…','busy');
  let transport;
  try {
    transport=button.dataset.demo==='transport'; const decision=button.dataset.demo==='decision';
    const [input,target,spec,schema]=await Promise.all([asset(transport?'transport.xlsx':'input.csv'),asset(transport?'transport-template.xlsx':decision?'decision-expected.csv':'template.csv'),asset(transport?'transport.yaml':'spec.yaml'),asset(decision?'decision-schema.json':'schema.json')]);
    let blob=await input.blob(); if(button.dataset.demo==='failure') blob=new Blob([(await blob.text()).replace('100.125','100.129')]);
    inputFile=new File([blob],transport?'freight-invoice.xlsx':'deliveries.csv'); templateFile=new File([await target.blob()],transport?'transport-template.xlsx':'expected.csv');
    schemaText=transport?'':await schema.text(); $('input-label').textContent=inputFile.name; $('template-label').textContent=templateFile.name;
    $('schema-label').textContent=schemaText?'Included delivery schema':'Choose target schema'; $('sheet-names').value='';
    exampleLoaded=true; $('target-role').value=decision?'expected':'template'; $('target-layout').value='auto';
    if(transport) { $('spec').value=await spec.text(); $('yaml-editor').open=true; }
    for(const demo of document.querySelectorAll('.demo')) demo.classList.toggle('selected',demo===button);
    status('Example ready. Review its rules, then validate.');
  } catch(e) { status(e.message,'bad'); }
  finally { busy=false; updateButtons(); }
  if(!transport && inputFile) await draftOrRestore();
});
function getWorker() {
  if(worker) return worker; worker=new Worker('worker.js',{type:'module'});
  worker.onmessage=({data}) => { if(data.progress){status(data.progress,'busy');return;} if(!pending||data.id!==pending.id)return; const {resolve,reject}=pending; pending=null; data.error?reject(new Error(data.error)):resolve(data.result); };
  worker.onerror=e => { worker.terminate();worker=null;if(pending){pending.reject(new Error(e.message||'Browser Python could not start.'));pending=null;} };
  return worker;
}
async function execute(action, extra={}) {
  if(busy) return; busy=true; invalidate(); status('Starting browser Python…','busy');
  try {
    const files=[{name:`input.${inputFile.name.split('.').pop().toLowerCase()}`,bytes:await inputFile.arrayBuffer()}]; let templateName;
    if(templateFile){templateName=`template.${templateFile.name.split('.').pop().toLowerCase()}`;files.push({name:templateName,bytes:await templateFile.arrayBuffer()});}
    const request={action,input:files[0].name,template:templateName,spec:$('spec').value,schema:schemaText,targetRole:$('target-role').value,targetLayout:$('target-layout').value,sheets:$('sheet-names').value.split('\n').map(s=>s.trim()).filter(Boolean),...(action==='draft'?{}:{config:workspace?.config}),...extra};
    const id=++requestId, result=await new Promise((resolve,reject)=>{pending={id,resolve,reject};getWorker().postMessage({id,files,request},files.map(f=>f.bytes));});
    if(result.config) {
      showWorkspace(result); if(action==='answer'&&!extra.manual) $('reply').value='';
      const unresolved=result.questions.length;
      status(`${extra.restored?'Cached config restored':'Config updated'}: ${unresolved} open question(s). ${unresolved?'Copy a prompt or enter decisions below.':'Preview & validate before approving.'}`,unresolved?'':'good');
    } else {
      evidence=result; validated=result.ok&&result.report.controls.some(c=>c.on_fail==='halt'&&!c.kind.startsWith('rule:'));
      if(action==='convert') result.report.browser_review={confirmed:true,at:new Date().toISOString()}; renderEvidence(result,action);
      status(result.ok?(action==='convert'?'Conversion complete. Download the result and its evidence.':validated?'Checks passed. Review the mapping and scope before converting.':'No halting reconciliation declared. Add controls or a complete expected result.'):'Validation stopped the run. Check open decisions and mismatches below.',result.ok?'good':'bad');
    }
  } catch(e) { validated=false;status(e.message,'bad');if(action==='answer') $('reply-notice').textContent=`Reply rejected. Config unchanged. ${e.message}`; }
  finally { busy=false;updateButtons(); }
}
$('preview').addEventListener('click',()=>execute('preview')); $('convert').addEventListener('click',()=>execute('convert'));
$('draft').addEventListener('click',()=>draftOrRestore().catch(e=>status(e.message,'bad')));
$('cancel').addEventListener('click',()=>{worker?.terminate();worker=null;if(pending){pending.reject(new Error('Stopped. Config is kept; the next run starts fresh Python.'));pending=null;}});
function renderPrompts() {
  const open=workspace.questions.length;
  $('resolve').hidden=!open&&!Object.keys(workspace.config.answers).length;
  $('resolve-title').textContent=open?'Resolve open questions':'Revise saved decisions';
  $('question-summary').textContent=open?`${open} decision(s) remain. Each config update clears prior validation and approval.`:'All questions are resolved. Paste a corrected JSON reply to replace saved decisions, then validate again.';
  $('prompt-tools').hidden=!workspace.promptPackets.length;
  $('manual-decisions').hidden=!open; $('resolve-grid').classList.toggle('single',!open);
  const previous=$('prompt-sheet').value; $('prompt-sheet').replaceChildren();
  for(const p of workspace.promptPackets){const o=node('option',p.sheet);o.value=p.sheet;$('prompt-sheet').append(o);}
  if(workspace.promptPackets.some(p=>p.sheet===previous))$('prompt-sheet').value=previous;
  $('prompt-text').value=workspace.promptPackets.find(p=>p.sheet===$('prompt-sheet').value)?.text||'';
}
$('prompt-sheet').addEventListener('change',()=>{$('prompt-text').value=workspace.promptPackets.find(p=>p.sheet===$('prompt-sheet').value)?.text||'';});
async function copyText(text,field='prompt-text',notice='reply-notice') { try { await navigator.clipboard.writeText(text); $(notice).textContent='Copied. Paste into your chosen chatbot; Sluice sends no request.'; } catch { $(field).focus();$(field).select();$(notice).textContent='Clipboard unavailable. Select the text and copy with Ctrl+C.'; } }
$('copy-prompt').addEventListener('click',()=>copyText($('prompt-text').value));
$('copy-reply-template').addEventListener('click',()=>copyText(JSON.stringify(workspace.promptPackets.find(p=>p.sheet===$('prompt-sheet').value)?.reply,null,2)));
$('repair-sheet').addEventListener('change',()=>{$('repair-prompt').value=evidence?.repairPrompts?.find(p=>p.sheet===$('repair-sheet').value)?.text||''; $('repair-notice').textContent='';});
$('copy-repair').addEventListener('click',()=>copyText($('repair-prompt').value,'repair-prompt','repair-notice'));
$('download-repair').addEventListener('click',()=>download('validation-repair-prompt.txt',$('repair-prompt').value,'text/plain'));
$('paste-correction').addEventListener('click',()=>{$('reply').scrollIntoView({block:'center'});$('reply').focus();});
function renderQuestions() {
  $('questions').replaceChildren();
  for(const q of workspace.questions) {
    const card=node('details',undefined,'question-card'), summary=node('summary',`${q.frame} · ${q.targets.join(', ')||q.id.split(':').pop()}`), form=node('div',undefined,'question-form'); card.append(summary,node('p',q.reason,'help'),form);
    const ctx=workspace.contexts[q.frame], scopeKind=q.id.endsWith(':grid_scope')?'grid_scope':'row_scope';
    const state=manualDrafts.get(q.id)||{kind:q.kind==='scope'?scopeKind:'constant',values:{}}; manualDrafts.set(q.id,state);
    const method=node('select'); method.className='manual-control'; method.setAttribute('aria-label',`Decision method for ${q.id}`);
    const kinds=q.kind==='scope'?[scopeKind,'advanced']:['constant','column','template','period','sheet_cell','advanced'];
    for(const kind of kinds){const o=node('option',({constant:'Constant — same on every row',column:'Copy / map a source column',template:'Text template / condition',period:'Extract a period',sheet_cell:'Use a banner cell',grid_scope:'Choose value columns to include',row_scope:'Choose source rows to include',advanced:'Advanced binding JSON'})[kind]);o.value=kind;method.append(o);} method.value=state.kind; form.append(method);
    const facts=node('details',undefined,'source-facts');facts.append(node('summary','Source names & sample values'),node('pre',JSON.stringify({names:ctx.labels,values:ctx.dimensions,cells:ctx.cells},null,2)));card.append(facts);
    const fields=node('div');form.append(fields);
    function input(label,key,choices,placeholder='') {
      const wrap=node('label',label,'small-label'), el=choices?node('select'):node('textarea'); el.className='manual-control';el.setAttribute('aria-label',`${label} for ${q.id}`);
      if(choices)for(const value of choices){const o=node('option',value);o.value=value;el.append(o);}else{el.rows=key==='bindings'?5:2;el.placeholder=placeholder;}
      el.value=state.values[key]??(choices?choices[0]:''); el.addEventListener('input',()=>{state.values[key]=el.value;}); el.addEventListener('change',()=>{state.values[key]=el.value;});wrap.append(el);fields.append(wrap);return el;
    }
    let inputs={};
    function draw(){ fields.replaceChildren();inputs={};
      if(method.value==='constant')inputs.value=input('Value','value',null,'Enter a value; leave blank only if intentionally empty.');
      if(['column','period'].includes(method.value))inputs.source=input('Source','source',ctx.sources);
      if(method.value==='column')inputs.mapping=input('Value mapping JSON (optional)','mapping',null,'{"source label":"target label"}');
      if(method.value==='template'){inputs.template=input('Template text','template',null,'{source_name}');inputs.where=input('Condition JSON (optional)','where',null,'{"source_name":"a value"}');}
      if(method.value==='period')inputs.part=input('Period part','part',['end','start','type']);
      if(method.value==='sheet_cell')inputs.cell=input('Banner cell','cell',Object.keys(ctx.cells));
      if(method.value==='grid_scope'){inputs.dimension=input('Header dimension','dimension',Object.keys(ctx.dimensions));inputs.include=input('Included values (one per line)','include',null,'Copy exact values from Source names & sample values.');}
      if(method.value==='row_scope'){inputs.source=input('Source','source',ctx.sources);inputs.operator=input('Condition','operator',['equals','in','not_in','regex','not_regex']);inputs.condition=input('Value / pattern (one per line for lists)','condition',null,'Enter the value or naming pattern that decides row scope.');}
      if(method.value==='advanced') inputs.bindings=input('Bindings JSON','bindings',null,q.kind==='scope'?'[{"kind":"row_scope","args":{"conditions":[{"column":"source_name","not_in":["total"]}],"mode":"all"}}]':'[{"kind":"column","target":"'+q.targets[0]+'","args":{"source":"source_name"}}]');
    }
    method.addEventListener('change',()=>{state.kind=method.value;draw();});draw();
    const submit=node('button','Apply manual decision','secondary wide manual-control');submit.type='button';form.append(submit);
    submit.addEventListener('click',()=>{try{
      let bindings;
      if(method.value==='advanced'){const parsed=JSON.parse(inputs.bindings.value);bindings=Array.isArray(parsed)?parsed:[parsed];}
      else { let args={};for(const [key,el]of Object.entries(inputs)){if(['mapping','where'].includes(key)){if(el.value.trim())args[key]=JSON.parse(el.value);}else args[key]=el.value;}
        if(method.value==='grid_scope')args.include=args.include.split('\n').filter(Boolean);
        if(method.value==='row_scope')args={conditions:[{column:args.source,[args.operator]:['in','not_in'].includes(args.operator)?args.condition.split('\n').filter(Boolean):args.condition}],mode:'all'};
        bindings=[{kind:method.value,target:q.kind==='scope'?null:q.targets[0],args,confidence:1}]; }
      execute('answer',{manual:true,reply:JSON.stringify({answers:[{question_id:q.id,status:'answered',bindings}]})});
    }catch(e){$('reply-notice').textContent=`Manual decision not applied: ${e.message}`;}});
    $('questions').append(card);
  }
}
function download(name,bytes,type='application/octet-stream'){const url=URL.createObjectURL(new Blob([bytes],{type})),a=document.createElement('a');a.href=url;a.download=name;a.hidden=true;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);}
function clearDownloads(){downloadUrls.forEach(url=>URL.revokeObjectURL(url));downloadUrls=[];}
$('download-config').addEventListener('click',event=>{if($('download-config').disabled)event.preventDefault();});
$('download-spec').addEventListener('click',()=>download('spec.yaml',$('spec').value,'text/yaml'));
$('download-prompts').addEventListener('click',()=>download('chat-prompts.txt',prompts.join('\n\n----------\n\n'),'text/plain'));
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
  const repairs=result.repairPrompts||[]; $('repair-feedback').hidden=!repairs.length;
  $('repair-sheet').replaceChildren(); for(const p of repairs){const o=node('option',p.sheet);o.value=p.sheet;$('repair-sheet').append(o);}
  $('repair-prompt').value=repairs[0]?.text||''; $('repair-notice').textContent='';
  if(repairs.length){$('resolve').hidden=false;if(!workspace.questions.length){$('resolve-title').textContent='Revise decisions';$('question-summary').textContent='Validation found a mismatch. Paste a corrected JSON reply, then validate again.';}}
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
