import{trials}from'../stores/trial.js';import{archiveStore}from'../stores/archive.js';import{header}from'../components/common/header.js';import{table}from'../components/common/table.js';import{badge}from'../components/common/badge.js';import{esc}from'../utils/html.js';import{notify}from'../hooks/feedback.js';
function blockRows(ev){return table(['块号','键区间','条目','压缩字节','校验'],ev.map(b=>[b.block_id,esc(b.min_key)+' … '+esc(b.max_key),b.entry_count,b.compressed_length,b.checksum_ok?'<span class="badge passed">通过</span>':'<span class="badge failed">失败</span>']))}
export async function archivePage(el){
  const list=(await trials.load()).filter(x=>x.status==='passed'&&!x.result?.error);
  const id=Number(location.hash.split('/')[1])||list[0]?.id;
  let meta=null;
  if(id){try{meta=await archiveStore.meta(id)}catch(e){notify(e.message)}}
  el.innerHTML=header('05','压缩档案范围查询 · 读盘计量与额度门控','仅通过 Oracle 校验的存活键值会被压缩归档；索引与档案同时就绪才开放查询。额度按“索引头 + 命中定位记录 + 命中压缩块”预估，不足即拒绝；命中块全部核验通过后才返回键值，坏块不交付半截结果。')
    +`<section class="panel"><label>选择已通过试验</label><select id="choose"><option value="">请选择试验</option>${list.map(x=>`<option value="${x.id}" ${x.id===id?'selected':''}>#${x.id} ${esc(x.name)}</option>`).join('')}</select></section>`
    +(id?renderMeta(id,meta):'<div class="empty">尚无通过校验的试验。先在合并队列完成一次试验。</div>');
  el.querySelector('#choose').onchange=e=>{if(e.target.value)location.hash='archive/'+e.target.value};
  const form=el.querySelector('#query-form');
  if(form)form.onsubmit=e=>{e.preventDefault();
    const d=Object.fromEntries(new FormData(e.target));
    const payload={start_key:d.start_key||null,end_key:d.end_key||null,read_quota:d.read_quota===''?null:Number(d.read_quota)};
    const out=el.querySelector('#query-out');
    out.innerHTML='<p class="helper">正在读取并核验命中块…</p>';
    archiveStore.rangeQuery(id,payload)
      .then(r=>renderResult(el,r))
      .catch(err=>renderFailure(el,err.message));
  };
}
function renderMeta(id,meta){
  if(!meta)return '<div class="empty">加载归档概况失败。</div>';
  if(!meta.archived)return `<section class="panel"><h2>旧试验 · 无分块档案</h2><p class="helper">${esc(meta.fallback||'该试验尚无分块压缩档案。')}</p><p>完整结果仍可在“读放大证据”页面与 JSON 导出中读取（存活键 ${meta.live_keys==null?'-':meta.live_keys} 个）。</p></section>`;
  return `<section class="panel"><div class="actions" style="margin-top:0"><h2 style="margin:0">试验 #${id} 压缩档案</h2>${badge('passed')}</div>
  <div class="metrics"><div><small>存活键 / 块数</small><strong>${meta.entry_count} / ${meta.block_count}</strong></div><div><small>原始 → 压缩字节</small><strong>${meta.raw_bytes} → ${meta.archive_bytes}</strong></div><div><small>压缩率</small><strong>${meta.compression_ratio} ×</strong></div><div><small>全档键区间</small><strong style="font-size:15px">${esc(meta.min_key)} … ${esc(meta.max_key)}</strong></div></div>
  <p class="helper">档案 ${esc(meta.archive_checksum.slice(0,16))}… ｜ 定位区 ${esc(meta.locator_checksum.slice(0,16))}… ｜ 索引头 ${meta.index_header_bytes} 字节，索引共 ${meta.index_bytes} 字节</p>
  <form id="query-form" style="margin-top:20px"><div class="split" style="gap:18px"><label>起始键（留空从头）<input name="start_key" placeholder="例如 sensor-010" maxlength="64"></label><label>结束键（留空到尾）<input name="end_key" placeholder="例如 sensor-050" maxlength="64"></label><label>读取额度（字节，留空不限）<input name="read_quota" type="number" min="0" placeholder="例如 1500"></label></div><div class="actions"><button class="primary" type="submit">按额度查询命中块 →</button></div><p class="helper">额度不足时本次查询直接拒绝，不读取任何数据块；坏块会让整次查询失败而不是返回部分键值。</p></form>
  <div id="query-out"></div></section>`;
}
function renderResult(el,r){
  const out=el.querySelector('#query-out');
  out.innerHTML=`<h2 style="margin-top:25px">查询命中 · 读盘证据</h2>
  <div class="metrics"><div><small>命中/读取块数</small><strong>${r.blocks_read}</strong></div><div><small>实际读盘字节</small><strong>${r.bytes_read}</strong></div><div><small>索引头 / 定位 / 数据块</small><strong style="font-size:16px">${r.index_header_bytes} / ${r.locator_bytes_read} / ${r.block_bytes_read}</strong></div><div><small>返回键数</small><strong>${r.key_count}</strong></div></div>
  <p class="helper">${esc(r.metering)}</p>
  <h2>命中块核验（全部通过后才交付）</h2>${blockRows(r.block_evidence)}
  <h2 style="margin-top:25px">键值结果</h2>${r.key_count?table(['键','值'],Object.entries(r.items).map(([k,v])=>[esc(k),esc(v)])):'<p class="empty">该范围没有存活键。</p>'}`;
}
function renderFailure(el,message){
  const out=el.querySelector('#query-out');
  out.innerHTML=`<h2 style="margin-top:25px">查询被拒绝</h2><p class="error">${esc(message)}</p><p class="helper">未交付任何键值：可能是读取额度不足（请提高额度或收窄范围），或命中块校验失败（档案损坏）。</p>`;
}
