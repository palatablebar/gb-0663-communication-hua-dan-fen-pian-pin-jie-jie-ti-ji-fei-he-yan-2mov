const feedback=document.getElementById('ledger-feedback');
function show(msg,ok=true){feedback.hidden=false;feedback.innerHTML=(ok?'[OK] ':'[ERROR] ')+msg;}
async function call(path,payload){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const data=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(data.detail||'请求失败');
  return data;
}
function decisionText(d){return {reopen:'重开原账',carry:'结转下期'}[d]||d||'—';}

document.getElementById('seed-btn').addEventListener('click',async()=>{
  try{
    const period=document.getElementById('seed-period').value.trim();
    const r=await call('/api/ledger/seed',{sample:'clean',period_id:period});
    show(`账期 ${r.period_id} 原账 v${r.version} 已建立，水位 ${r.watermark.fragment_count} 片，金额 ${r.summary.total_fee} CNY；即将刷新`);
    setTimeout(()=>location.reload(),900);
  }catch(e){show(e.message,false);}
});

document.getElementById('supp-demo').addEventListener('click',async()=>{
  const resp=await fetch('/api/ledger/demo-supplement');
  document.getElementById('supp-text').value=await resp.text();
});

document.getElementById('supp-send').addEventListener('click',async()=>{
  const text=document.getElementById('supp-text').value;
  const decision=document.getElementById('decision').value;
  if(!text.trim()){show('补片CSV不能为空',false);return;}
  try{
    const r=await call('/api/ledger/supplements',{csv_text:text,decision:decision||null});
    if(r.status==='DUPLICATE'){show(`同一补片/分片已在水位内，未重复累计（digest ${r.digest.slice(0,10)}…）`);return;}
    if(r.status==='REJECTED'){show('补片无有效分片，已隔离并保留原始记录，账未变化',false);return;}
    const dtotal=r.deltas.filter(d=>Number(d.delta)!==0).length;
    if(r.decision==='reopen'){
      show(`重开原账：账期 ${r.period_id} 生成 v${r.version}，${dtotal} 个会话差额，差额来源已登记；即将刷新`);
    }else{
      const unsafe=r.unsafe_reopen?`；重开被拒（${r.unsafe_reopen}）`:'';
      show(`保留原账 v${r.version}，差额结转到 ${r.to_period}（${r.adjustments.length} 行调整）${unsafe}；即将刷新`);
    }
    setTimeout(()=>location.reload(),1100);
  }catch(e){show(e.message,false);}
});

let selectedPeriod=document.getElementById('seed-period').value.trim();
document.querySelectorAll('.period-link').forEach(a=>a.addEventListener('click',async e=>{
  e.preventDefault();
  const period=e.currentTarget.closest('.period-row').dataset.period;
  selectedPeriod=period;
  const r=await (await fetch('/api/ledger/periods/'+period)).json();
  renderDetail(period,r);
}));

document.getElementById('issue-btn').addEventListener('click',async()=>{
  const period=selectedPeriod;
  try{const r=await call('/api/ledger/issue',{period_id:period});show(`账期 ${r.period_id} 已出账（${r.state}），此后补片只能结转下期`);setTimeout(()=>location.reload(),900);}
  catch(e){show(e.message,false);}
});

function renderDetail(period,r){
  document.getElementById('detail-panel').hidden=false;
  document.getElementById('detail-title').textContent=period;
  document.getElementById('detail-state').textContent=`${r.period.state} / v${r.current_version} / 上期结转 ${r.carry_in_total} CNY`;
  const b=r.bill;
  document.getElementById('detail-kpis').innerHTML=[
    ['水位分片',b.watermark.fragment_count],['水位摘要',b.watermark.fragment_digest.slice(0,16)+'…'],
    ['纳入补片',b.watermark.supplements.length],['当前金额',b.summary.total_fee+' CNY'],
    ['计费分钟',b.summary.minutes],['隔离分片',b.summary.quarantined]
  ].map(([k,v])=>`<div class="kpi"><label>${k}</label><strong>${v}</strong></div>`).join('');
  document.querySelector('#delta-table tbody').innerHTML=r.deltas.map(d=>
    `<tr><td>${d.session}</td><td class="num">${d.old_fee||'—'}</td><td class="num">${d.new_fee}</td>
     <td class="num ${Number(d.delta)>0?'bad':''}">${d.delta}</td>
     <td class="num">${d.old_seconds||'—'} → ${d.new_seconds}</td>
     <td class="num">${d.old_minutes??'—'} → ${d.new_minutes}</td><td>${d.source}</td></tr>`).join('')
     ||'<tr><td colspan="7" class="empty">[EMPTY] 原账版本，无差额</td></tr>';
  document.querySelector('#adj-table tbody').innerHTML=r.adjustments_in.map(a=>
    `<tr><td>${a.period_id}</td><td>${a.from_period}</td><td>${a.session}</td><td class="num">${a.amount}</td><td>${a.source}</td></tr>`).join('')
     ||'<tr><td colspan="5" class="empty">[EMPTY] 无结转调整</td></tr>';
  document.getElementById('delta-export').href=`/api/ledger/export/deltas/${period}`;
  document.getElementById('detail-panel').scrollIntoView({behavior:'smooth'});
}
