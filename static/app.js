const $ = s => document.querySelector(s);
let state, kind='disaster', busy=false, csrf='', lastView='', requestId=crypto.randomUUID(), flash='', pendingAction='';
const escape = v => String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names={disaster:'Disaster scenario',partial:'Partial outage',single:'Persistent pod failure'};
async function trace(view,outcome='visible') { if(!csrf)return;try{await fetch('/api/trace',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,'X-Correlation-ID':requestId},body:JSON.stringify({state:view,outcome})})}catch{}}
function plan(){
  if(!state)return;
  $('#appField').hidden=kind==='disaster';$('#countField').hidden=kind!=='partial';$('#podField').hidden=kind!=='single';
  $('#planTitle').textContent=names[kind];
  const app=state.apps.find(a=>a.id===$('#app').value);
  const count=kind==='disaster'?state.apps.reduce((n,a)=>n+Math.ceil(a.pods.length*.75),0):kind==='single'?1:Number($('#count').value);
  $('#plan').textContent=`${count} pod${count===1?'':'s'} targeted ${kind==='disaster'?`across ${state.apps.length} applications`:`in ${app?.name||'selected application'}`}. Image-pull failures are held until reset.`;
  const old=$('#pod').value;$('#pod').innerHTML=(app?.pods||[]).map(p=>`<option value="${escape(p.name)}">${escape(p.name)}</option>`).join('');if([...$('#pod').options].some(o=>o.value===old))$('#pod').value=old;
  $('#apply').disabled=busy||!state.baseline||!!state.active||state.phase!=='normal'||!count||!state.apps.length;
}
function render(){
  const pods=state.apps.flatMap(a=>a.pods),faults=pods.filter(p=>Object.values(p.images).some(i=>i.includes('rca-scenario.invalid/')));
  $('#mode').textContent=state.mode==='demo'?'DEMO ENVIRONMENT':'LIVE KUBERNETES';
  $('#healthBadge').textContent=state.mode==='demo'?'SIMULATED OBSERVATIONS':'LIVE OBSERVATIONS';
  $('#appsCount').textContent=state.apps.length;$('#readyCount').textContent=pods.filter(p=>p.ready).length;$('#faultCount').textContent=faults.length;$('#totalCount').textContent=`of ${pods.length} observed pods`;$('#scope').textContent=state.scope.join(', ');$('#context').textContent=state.context;
  $('#phase').textContent=state.phase.replaceAll('-',' ');$('#notice').classList.toggle('error',!!state.error||!!flash);
  $('#notice').textContent=(busy?`Working: ${pendingAction}…`:null)||flash||state.error||(state.mode==='demo'?'Demo mode — these apps are simulated. Connect your Kubernetes context and namespaces to affect BootstrapKubernetes.':`Live scope: ${state.context} / ${state.scope.join(', ')}. Use BootstrapKubernetes’s RCA button after failures are observed.`);
  $('#applications').innerHTML=state.apps.map(a=>{const ready=a.pods.filter(p=>p.ready).length;return `<tr><td>${escape(a.name)}<small>${escape(a.namespace)} · Deployment</small></td><td><div class="pods">${a.pods.map(p=>`<span class="pod ${p.ready?'':p.reason==='ImagePullBackOff'||p.reason==='ErrImagePull'?'down':'pending'}" title="${escape(p.name+' · '+p.reason)}" aria-label="${escape(p.name+' '+p.reason)}"></span>`).join('')}</div></td><td>${ready} <span>/</span> ${a.desired}</td><td><span class="health ${ready<a.desired?'bad':''}">${ready<a.desired?'● Degraded':'● Healthy'}</span></td></tr>`}).join('');
  $('#empty').hidden=!!state.apps.length;
  $('#baseline').textContent=state.baseline?`Captured ${new Date(state.baseline.time).toLocaleString()} · ${state.baseline.apps.length} applications`:'No baseline captured. Save a healthy state before injecting failures.';
  $('#historyCount').textContent=`${state.history.length} runs`;
  $('#history').innerHTML=state.history.length?state.history.map(h=>`<div class="history-item">${escape(names[h.scenario])} · ${h.ended?'Restored':'Active'}<small>${escape(new Date(h.started).toLocaleString())} · ${escape(h.id)}</small><small>Expected cause: injected invalid container image</small></div>`).join(''):'<p>No scenarios yet. Start with a healthy baseline.</p>';
  const selected=$('#app').value;$('#app').innerHTML=state.apps.map(a=>`<option value="${escape(a.id)}">${escape(a.name)}</option>`).join('');if([...$('#app').options].some(o=>o.value===selected))$('#app').value=selected;
  $('#capture').disabled=busy||!!state.active||state.journal.length>0||state.phase!=='normal';$('#reset').disabled=busy||(!state.active&&!state.journal.length&&state.phase==='normal');
  $('#updated').textContent=`Observed ${new Date().toLocaleTimeString()}`;plan();
  const view=state.phase+':'+pods.filter(p=>p.ready).length+':'+faults.length+':'+(state.error?'error':'ok');if(view!==lastView){lastView=view;trace(view)}
}
async function refresh(){try{const r=await fetch('/api/state');const data=await r.json();if(!r.ok)throw Error(data.error);state=data;csrf=data.csrf;render();}catch(e){$('#notice').textContent=e.message||'Controller unreachable';$('#notice').classList.add('error');$('#updated').textContent='Connection lost · data may be stale';['apply','capture','reset'].forEach(k=>$('#'+k).disabled=true);trace('connection-error','error')}}
async function action(path,body={}){if(busy)return;busy=true;pendingAction=path;flash='';requestId=crypto.randomUUID();render();trace('loading:'+path);try{const r=await fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,'X-Correlation-ID':requestId},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(data.error);/* Visible outcome is recorded by render after the state refresh. */}catch(e){flash=e.message;busy=false;render();await trace('failed:'+path,'error')}finally{busy=false;await refresh()}}
document.querySelectorAll('.scenario').forEach(el=>el.onclick=()=>{kind=el.dataset.kind;document.querySelectorAll('.scenario').forEach(b=>b.classList.toggle('selected',b===el));plan();trace('scenario-selected:'+kind)});
$('#app').onchange=plan;$('#count').oninput=plan;$('#capture').onclick=()=>action('capture');$('#reset').onclick=()=>action('reset');$('#apply').onclick=()=>{if(state.mode==='kubernetes'&&!confirm(`Apply ${names[kind]} to ${state.context}? ${$('#plan').textContent}`))return;action('apply',{scenario:kind,app:$('#app').value,count:Number($('#count').value),pod:$('#pod').value})};
refresh();setInterval(()=>{if(!busy)refresh()},5000);
