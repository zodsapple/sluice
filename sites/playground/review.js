import {describeBinding, feedbackPrompt} from './review-text.mjs';
const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names = {period_end:'日期',report_row_label:'记录标签',revision_status:'修订状态',reporting_year:'报告年份',grid_scope:'表格列范围',row_scope:'记录范围'};
const CACHE = 'sluice-review-usda-v1';
let fixture, config, evidence, workspace, worker, pending, busy = false, id = 0, filter = '', selected;
let promptState, proposal, replyError = '';
const version = () => JSON.stringify(config);
const feedback = () => $('feedback').value.trim();
const promptCurrent = () => promptState && promptState.version===version() && promptState.feedback===feedback();

function status(message, kind = '') { $('status').textContent = message; $('status').className = kind; }
function buttons() {
  for (const el of document.querySelectorAll('button, input, textarea:not([readonly])')) el.disabled = busy;
  $('suggest').disabled = busy || !evidence || !feedback();
  $('copy-prompt').disabled = busy || !promptCurrent();
  $('parse-reply').disabled = busy || !promptCurrent() || !$('reply').value.trim();
  $('apply').disabled = busy || !proposal;
  $('discard').disabled = busy || !proposal;
  $('fix-reply').disabled = busy || !replyError || !promptCurrent();
  $('export-result').disabled = busy || !evidence?.ok || Boolean(proposal) || !$('approved').checked;
}
function clearProposal() { proposal=undefined; $('proposal').hidden=true; }
function clearExchange() {
  clearProposal(); promptState=undefined; replyError=''; $('reply').value=''; $('prompt').value='';
  $('reply-notice').textContent=''; $('fix-reply').hidden=true; $('exchange').hidden=true;
}
function invalidate() { evidence=undefined; $('approved').checked=false; }
function commit(result) {
  config=result.config; workspace=result;
  invalidate(); clearExchange();
  $('config-view').textContent=JSON.stringify(config,null,2);
  try { sessionStorage.setItem(CACHE,JSON.stringify({config})); }
  catch { $('cache-note').textContent='Session 存储不可用；请下载转换规则以便恢复。'; }
}
async function asset(name,type='json') {
  const r=await fetch(`assets/${name}`); if(!r.ok) throw new Error(`无法载入样例文件：${name}`);
  return type==='json'?r.json():r.arrayBuffer();
}
async function getFixture() {
  if(fixture) return fixture;
  const [data,input,target]=await Promise.all([asset('review-usda.json'),asset('review-usda.xlsx','bytes'),asset('review-expected.csv','bytes')]);
  fixture={initialConfig:data.initialConfig,input,target}; return fixture;
}
function call(action,extra={}) {
  if(!worker) {
    worker=new Worker('worker.js',{type:'module'});
    worker.onmessage=({data})=>{
      if(data.progress){status(data.progress,'busy');return;}
      if(!pending || pending.id!==data.id)return;
      const p=pending;pending=undefined; data.error?p.reject(new Error(data.error)):p.resolve(data.result);
    };
    worker.onerror=e=>{pending?.reject(new Error(e.message || 'Python runtime 启动失败，请重试。'));pending=undefined;worker.terminate();worker=undefined;};
  }
  const files=[{name:'input.xlsx',bytes:fixture.input.slice(0)},{name:'expected.csv',bytes:fixture.target.slice(0)}];
  const request={action,input:'input.xlsx',template:'expected.csv',targetRole:'expected',targetLayout:'auto',guided:true,config:structuredClone(config),...extra};
  return new Promise((resolve,reject)=>{pending={id:++id,resolve,reject};worker.postMessage({id,files,request},files.map(f=>f.bytes));});
}
async function task(work) {
  if(busy)return;busy=true;buttons();
  try{await work();}catch(e){status(`操作未完成：${e.message}。当前规则保留，请重试。`,'error');}
  finally{busy=false;buttons();}
}
const expectedControl = result => result?.report.controls.find(c=>c.name==='expected_result');
const unresolved = () => workspace?.questions?.length || 0;
const format = value => String(value ?? '').replace(/T00:00:00$/,'') || '（空）';
function differences() {
  return (expectedControl(evidence)?.field_differences || []).flatMap(d=>Object.entries(d.fields).map(([field,values])=>({...d.source,field,...values})));
}
function renderDiffs() {
  const diffs=differences(), fields=[...new Set(diffs.map(d=>d.field))];if(!fields.includes(filter))filter='';
  $('filters').innerHTML=['',...fields].map(f=>`<button data-filter="${escape(f)}" class="${f===filter?'active':''}" aria-pressed="${f===filter}">${escape(names[f] || f || '所有差异')}</button>`).join('');
  const show=diffs.map((d,i)=>({...d,i})).filter(d=>!filter || d.field===filter);
  $('diff-rows').innerHTML=show.map(d=>`<tr><td>${escape(d.source_cell || d.source_row)} <span class="tiny">${escape(d.source_sheet)}</span></td><td>${escape(names[d.field] || d.field)}</td><td class="actual">${escape(format(d.actual))}</td><td class="expected">${escape(format(d.expected))}</td><td><button class="row-select" data-diff="${d.i}" aria-label="选择 ${escape(d.source_cell)} 的${escape(names[d.field] || d.field)}差异">指出这处</button></td></tr>`).join('');
  if(!show.length) {
    $('diff-rows').innerHTML=`<tr><td colspan="5" class="empty-row">${evidence?.ok?'没有剩余差异。':expectedControl(evidence)?.passed?'数据已匹配，仍需补充未确定的规则。':'没有可靠对应的字段差异；下面分别展示缺少和多出的记录。'}</td></tr>`;
    if(expectedControl(evidence) && !expectedControl(evidence).passed) {
      for(const [kind,title] of [['missing','缺少的目标记录'],['unexpected','多出的输出记录']]) {
        for(const example of expectedControl(evidence).examples?.[kind] || []) {
          const values=Object.entries(example.row).map(([field,v])=>`${names[field] || field}：${format(v)}`).join('；');
          $('diff-rows').insertAdjacentHTML('beforeend',`<tr><td colspan="5" class="record-example">${title}：${escape(values)}</td></tr>`);
        }
      }
    }
  }
  $('passed-note').hidden=!evidence?.ok;
}
function render() {
  const control=expectedControl(evidence), open=unresolved();
  $('total').textContent=control?.expected ?? '—';
  $('matched').textContent=open?'待校验':control?Math.max(0,control.expected-control.dropped):'—';
  $('different').textContent=open?'待补充':control?.dropped ?? '—';
  $('decisions').textContent=open?`${open} 项待补充`:'无待补充项';
  $('headline').textContent=open?'补充业务决定后，才能完成校验。':evidence.ok?'54 条记录，全部匹配。':'文件读进来了，还有几处需要你指出。';
  $('finish-title').textContent=evidence.ok?'校验通过。请确认你理解这次转换规则。':'规则值得保留，结果需要检查。';
  $('open-decisions').hidden=!open;
  $('question-list').textContent=(workspace.questions || []).map(q=>`${q.frame}：${q.targets.map(t=>names[t] || t).join('、') || names[q.id.split(':').pop()] || q.id}`).join('；');
  const preview=evidence.outputPreview;
  $('output-preview').innerHTML=preview?`<table><thead><tr>${preview.columns.map(c=>`<th>${escape(c)}</th>`).join('')}</tr></thead><tbody>${preview.rows.map(row=>`<tr>${row.map(v=>`<td>${escape(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`:'<p>暂无输出样例。</p>';
  selected=undefined;$('selected-evidence').hidden=true;renderDiffs();
  document.querySelectorAll('.steps li').forEach((el,i)=>el.classList.toggle('active',i===(evidence.ok?3:1)));
}
async function validate() {
  invalidate();status('正在本地重新转换，并检查完整 expected…','busy');
  evidence=await call('preview');render();const control=expectedControl(evidence);
  status(unresolved()?`还有 ${unresolved()} 项决定待补充，导出保持关闭。`:evidence.ok?'真实校验通过：54 / 54 条记录匹配。':`真实校验：${control?control.expected-control.dropped:0} 条匹配；${control?.dropped ?? '未知'} 条存在差异。`,evidence.ok?'':'error');
}
async function load() {
  await getFixture();let saved;try{saved=JSON.parse(sessionStorage.getItem(CACHE) || 'null');}catch{}
  config=structuredClone(saved?.config || fixture.initialConfig);status('正在检查文件与配置…','busy');
  commit(await call('load_config'));document.body.classList.add('reviewing');$('workspace').hidden=false;
  $('load').textContent='重新载入（恢复 session 规则）';await validate();
  if(saved)status(`Session 规则已恢复并重新校验。${evidence.ok?'54 / 54 条记录匹配。':'请继续处理差异或待补充项。'}`);
}
async function copy(text) {
  try{await navigator.clipboard.writeText(text);return true;}catch{
    $('prompt-details').open=true;$('prompt').focus();$('prompt').select();return false;
  }
}
async function makePrompt() {
  const base=evidence?.reviewPrompts?.[0]?.text || evidence?.repairPrompts?.[0]?.text;
  if(!base || !feedback())return;
  clearProposal();replyError='';$('fix-reply').hidden=true;$('reply-notice').textContent='';$('reply').value='';$('approved').checked=false;
  promptState={version:version(),feedback:feedback()};$('prompt').value=feedbackPrompt(base,feedback(),selected);$('exchange').hidden=false;
  const copied=await copy($('prompt').value);
  status(copied?'Prompt 已复制。粘贴到你的 chatbot，再把完整回复粘贴到下方。':'Prompt 已生成并选中，请按 Ctrl+C，再粘贴到你的 chatbot。');buttons();
}
function explain(bindings) {
  return bindings.length?`<ul class="binding-summary">${bindings.map(b=>`<li>${escape(describeBinding(b))}</li>`).join('')}</ul>`:'<p class="help">原先的自动推断规则；请对照结果样例。</p>';
}
function renderProposal() {
  const result=proposal.result, current=expectedControl(evidence), next=expectedControl(result.preview), impact=result.impact;
  $('proposal-summary').textContent=`本次回复有 ${result.changes.length} 项规则变化。整组确认后才会更新配置，未涉及的规则保持原样。`;
  const counts=current && next?`目标匹配 ${current.expected-current.dropped} → ${next.expected-next.dropped} 条。`:'';
  $('proposal-impact').textContent=`试算，尚未应用：${counts} ${impact.paired?`修改 ${impact.changedRows} 条记录，新增 ${impact.addedRows} 条，移除 ${impact.removedRows} 条。`:`输出 ${impact.rowsBefore ?? '未知'} → ${impact.rowsAfter ?? '未知'} 条，无法按来源可靠对应逐行变化。`} ${result.preview.ok?'候选规则已通过本地校验。':`候选规则仍需继续修正${result.questions.length?`，有 ${result.questions.length} 项待补充`:''}。`}`;
  $('proposal-cards').innerHTML=result.changes.map(change=>{
    const examples=(change.examples || []).map(e=>`<div class="change-example"><span>${escape(Object.values(e.source).join(' · '))}</span><div class="before-after"><del>${escape(format(e.before))}</del><span>→</span><strong>${escape(format(e.after))}</strong></div></div>`).join('');
    return `<article class="proposal-item"><h3>${escape(names[change.field] || change.field)}</h3><p class="small">${escape(change.sheet)}</p><strong class="summary-label">修改后</strong>${change.status==='unknown'?'<p class="deferred">回复仍不确定；确认后将标记为待补充并阻止导出。</p>':explain(change.after)}<details><summary>查看原来的规则</summary>${explain(change.before)}</details><p class="scope">${change.affectedRows===undefined?'修改此字段或筛选范围，具体效果见整组试算。':`该字段实际改变 ${change.affectedRows} 条记录。`}</p>${examples}</article>`;
  }).join('');
  $('proposal-notices').textContent=result.notices.join('\n');$('proposal').hidden=false;
  $('proposal').scrollIntoView({behavior:'smooth',block:'start'});
}
async function parseReply() {
  if(!promptCurrent())throw new Error('反馈或规则已变化，请重新生成 prompt');
  const text=$('reply').value;if(text.length>1024*1024)throw new Error('回复超过 1 MB，请让 chatbot 仅返回需要修改的决定');
  clearProposal();$('approved').checked=false;replyError='';$('fix-reply').hidden=true;
  status('正在检查回复、试算真实改动；当前配置尚未修改…','busy');
  try {
    const result=await call('prepare_reply',{reply:text});
    proposal={result,reply:text,version:version(),feedback:feedback()};
    $('reply').value='';$('reply-notice').textContent=`已读取回复：${result.changes.length} 项待确认改动。当前配置尚未修改。`;
    renderProposal();status('实际修正建议已生成。检查规则、样例和试算结果，再决定是否应用。');
  } catch(e) {
    replyError=e.message;$('fix-reply').hidden=false;
    $('reply-notice').textContent=`回复未被接受，配置保持原样。${e.message}。可以复制修正提示，让 chatbot 重答。`;
    throw e;
  }
}
async function applySuggestions() {
  if(!proposal || proposal.version!==version() || proposal.feedback!==feedback() || !promptCurrent())throw new Error('反馈或规则已经变更，请重新查看回复建议');
  status('确认这组规则，并重新校验…','busy');
  const result=await call('answer',{reply:proposal.reply});commit(result);await validate();
}
function download(bytes,name,type) {
  const url=URL.createObjectURL(new Blob([bytes],{type}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1500);
}
$('load').onclick=()=>task(load);
$('reset').onclick=()=>task(async()=>{sessionStorage.removeItem(CACHE);$('feedback').value='';await load();});
$('filters').onclick=e=>{const button=e.target.closest('[data-filter]');if(button){filter=button.dataset.filter;renderDiffs();}};
$('diff-rows').onclick=e=>{
  const button=e.target.closest('[data-diff]');if(!button)return;selected=differences()[Number(button.dataset.diff)];clearExchange();
  $('selected-evidence').textContent=`已选择 ${selected.source_cell} 的${names[selected.field] || selected.field}：${format(selected.actual)} → ${format(selected.expected)}。请补充原因。`;
  $('selected-evidence').hidden=false;
  if(!feedback())$('feedback').value=`${selected.source_cell} 的${names[selected.field] || selected.field}应该是 ${format(selected.expected)}。原因：`;
  $('approved').checked=false;buttons();$('feedback').focus();
};
$('example-feedback').onclick=()=>{
  $('feedback').value='美国港口和 Mexico 使用各自表格的日期与行标签，不能混用。Mexico 没有 p/r 标记，其修订状态应为 reported。比较行的 reporting_year 使用报告年份 2022，历史累计与年度行保留其所属年份。';
  clearExchange();$('approved').checked=false;buttons();
};
$('feedback').oninput=()=>{clearExchange();$('approved').checked=false;buttons();};
$('suggest').onclick=makePrompt;
$('copy-prompt').onclick=async()=>{const ok=await copy($('prompt').value);status(ok?'Prompt 已复制，请粘贴到你的 chatbot。':'请按 Ctrl+C 复制已选中的 prompt。');};
$('reply').oninput=()=>{clearProposal();replyError='';$('fix-reply').hidden=true;$('reply-notice').textContent='';$('approved').checked=false;buttons();};
$('parse-reply').onclick=()=>task(parseReply);
$('apply').onclick=()=>task(applySuggestions);
$('discard').onclick=()=>{clearProposal();$('reply-notice').textContent='已放弃这组建议。当前规则未变，可继续向 chatbot 补充反馈。';buttons();};
$('fix-reply').onclick=async()=>{
  const text=`${$('prompt').value}\n\n### PREVIOUS REPLY WAS REJECTED\nFix the reply format and permitted bindings using the original instructions. Do not weaken validation.\n${JSON.stringify({local_error:replyError,previous_reply:$('reply').value})}`;
  $('prompt').value=text;const ok=await copy(text);status(ok?'回复修正提示已复制，请让 chatbot 重答，再粘贴新的完整回复。':'修正提示已选中，请按 Ctrl+C 复制。');
};
$('approved').onchange=buttons;
$('export-config').onclick=()=>download(JSON.stringify(config,null,2),'sluice-usda-config.json','application/json');
$('export-result').onclick=()=>task(async()=>{
  if(!evidence?.ok || proposal || !$('approved').checked)throw new Error('请先确认规则并通过校验');
  const result=await call('convert');if(!result.ok || !result.download)throw new Error('校验未通过，无法导出');
  download(result.download.bytes,'usda-converted.csv','text/csv');status('CSV 已生成，54 条记录通过真实校验。');
});
buttons();
