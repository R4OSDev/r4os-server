"use strict";

window.R4 = (() => {
  const content = document.querySelector('#content-area');
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {queued:'Queued for validation',receiving:'Receiving',validating:'Validating',valid:'Valid',invalid:'Invalid',published:'Published',completed:'Completed',failed:'Failed',incomplete:'Incomplete',cancelled:'Cancelled',running:'Running',pending:'Reference missing',unscored:'Unscored',unavailable:'Unavailable',ok:'OK',skipped:'Skipped'};
  const ui = {views:{},generation:0,busy:false,poll:null,current:null,esc,labels};
  const errors={unauthorized:'Access denied.',forbidden:'You do not have permission for this action.',backend_unavailable:'The service is unavailable.',validation_error:'Check the entered values.',not_found:'The requested item was not found.',server_not_configured:'The server is not configured.',request_timeout:'The request timed out.',payload_too_large:'The uploaded data is too large.',unsupported_media_type:'This file type is not supported.',origin_not_allowed:'This request origin is not allowed.',origin_required:'A same-origin request is required.',host_not_allowed:'This server address is not allowed.',method_not_allowed:'This action is not supported.',invalid_range:'This download range is invalid.',output_missing:'The local output file is missing.',output_not_found:'The output file was not found.',configuration_changed:'The configuration changed. Reload it.'};
  ui.status = (message, error=false) => {
    const node = document.querySelector('#status-message');
    node.textContent=message; node.title=message; node.classList.toggle('is-error',error);
  };
  ui.count = value => { if(value===null || value===undefined) return '—'; try{return BigInt(value).toLocaleString('en-GB');}catch{return String(value);} };
  ui.number = value => {if(value===null || value===undefined)return '—';const number=Number(value);if(!Number.isFinite(number))return String(value);return number.toLocaleString('en-GB',number!==0 && (Math.abs(number)<.0001 || Math.abs(number)>1e12)?{notation:'scientific',maximumFractionDigits:3}:{maximumFractionDigits:3});};
  ui.date = value => value ? new Date(value).toLocaleString('en-GB',{timeZone:'UTC',dateStyle:'medium',timeStyle:'short'})+' UTC' : '—';
  ui.bytes = value => value===null || value===undefined ? '—' : Number(value)<1048576 ? ui.number(Number(value)/1024)+' KiB' : ui.number(Number(value)/1048576)+' MiB';
  ui.badge = status => '<span class="badge badge-'+esc(status)+'">'+esc(labels[status] || status || '—')+'</span>';
  ui.raw = value => '<pre class="raw-data">'+esc(JSON.stringify(value,null,2))+'</pre>';
  ui.fold = (title,value) => '<details class="data-fold"><summary>'+esc(title)+'</summary>'+ui.raw(value)+'</details>';
  ui.empty = (title,text='') => '<div class="empty-state"><span class="empty-mark" aria-hidden="true">◇</span><h2>'+esc(title)+'</h2><p>'+esc(text)+'</p></div>';
  ui.table = (headers,rows) => '<div class="table-scroll"><table><thead><tr>'+headers.map(v=>'<th>'+esc(v)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map(v=>'<td>'+v+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';
  ui.record = (html,actions='',attributes='',className='') => '<article class="record'+(className?' '+esc(className):'')+'" role="listitem"'+attributes+'><div class="record-content">'+html+'</div>'+(actions?'<div class="record-actions">'+actions+'</div>':'')+'</article>';
  ui.records = (items,label='',attributes='') => '<div class="record-list" role="list"'+(label?' aria-label="'+esc(label)+'"':'')+attributes+'>'+items.join('')+'</div>';
  ui.fields = items => '<dl class="record-fields">'+items.map(([label,html])=>'<div><dt>'+esc(label)+'</dt><dd>'+html+'</dd></div>').join('')+'</dl>';
  ui.card = (label,value,note='') => '<div class="metric-card"><span>'+esc(label)+'</span><strong>'+esc(value)+'</strong><small>'+esc(note)+'</small></div>';
  ui.show = (ctx,html) => {if(ctx.generation===ui.generation) content.innerHTML=html;};
  ui.scrollContainer = () => content.querySelector('.page-scroll') || content;
  ui.refreshIcon = '<svg class="button-icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#icon-refresh"/></svg>';
  ui.page = (ctx,title,description,html,actions='',refresh=true) => {
    // A supplied refresh button keeps page-specific actions such as catalogue refresh.
    const refreshButton=typeof refresh==='string'?refresh:refresh?'<button type="button" class="button subtle" data-action="refresh">'+ui.refreshIcon+'<span>Refresh</span></button>':'';
    const toolbar=refreshButton||actions?'<header class="page-toolbar"><div class="page-toolbar-inner" role="toolbar" aria-label="Page actions">'+(refreshButton?'<div class="page-refresh">'+refreshButton+'</div>':'')+(actions?'<div class="page-actions">'+actions+'</div>':'')+'</div></header>':'';
    ui.show(ctx,toolbar+'<div class="page-scroll" role="region" aria-label="'+esc(title||'Page')+' content" tabindex="0"><section class="feature-page">'+html+'</section></div>');
  };
  ui.side = (ctx,items) => {
    const label={apps:'Apps',updates:'Update Center',benchmarks:'Benchmarks',repositories:'Repositories',settings:'Settings'}[ctx.view];
    document.querySelector('.submenu-column:not(.third-column)').innerHTML='<nav class="secondary-navigation" aria-label="'+esc(label)+' views">'+items.map(([id,label])=>'<a class="sub-item" href="#'+ctx.view+'/'+id+'"'+(ctx.sub===id?' aria-current="page"':'')+'>'+esc(label)+'</a>').join('')+'</nav>';
  };
  ui.context = (ctx,items=[]) => {
    document.querySelector('.third-column').innerHTML=items.length?'<nav class="secondary-navigation" aria-label="Selection">'+items.map(([id,label])=>'<a class="sub-item" href="#'+ctx.view+'/'+ctx.sub+'/'+encodeURIComponent(id)+'"'+((ctx.leaf||'all')===id?' aria-current="page"':'')+'>'+esc(label)+'</a>').join('')+'</nav>':'';
  };
  ui.api = async (path,options={}) => {
    const controller=new AbortController(); const timeout=setTimeout(()=>controller.abort(),35000);
    const headers={Accept:'application/json',...(options.headers||{})};
    if(options.method && !['GET','HEAD'].includes(options.method)) headers['X-R4OS-Request']='1';
    if(options.body!==undefined && typeof options.body!=='string') {options={...options,body:JSON.stringify(options.body)};headers['Content-Type']='application/json';}
    try {
      const response=await fetch('/api/'+path,{cache:'no-store',credentials:'same-origin',...options,headers,signal:controller.signal});
      const result=await response.json();
      if(!response.ok) {const error=new Error(result.detail || errors[result.error] || (result.error?String(result.error).replaceAll('_',' '):'Request failed.')); error.status=response.status;throw error;}
      return result;
    } catch(error) {if(error.name==='AbortError') throw new Error('The service did not respond in time.');throw error;}
    finally {clearTimeout(timeout);}
  };
  ui.open = (title,html) => {
    let dialog=document.querySelector('#detail-dialog');
    if(!dialog){dialog=document.createElement('dialog');dialog.id='detail-dialog';dialog.className='detail-dialog';document.body.append(dialog);}
    dialog.innerHTML='<div class="dialog-heading"><h2>'+esc(title)+'</h2><button type="button" class="button icon-button" data-action="close-dialog" aria-label="Close">×</button></div><div class="dialog-body">'+html+'</div>';
    if(!dialog.open) dialog.showModal();
  };
  ui.navigate = async () => {
    clearTimeout(ui.poll);
    document.querySelector('#detail-dialog')?.close();
    const parts=location.hash.slice(1).split('/');
    let view=parts[0] || 'apps';if(!['apps','updates','benchmarks','repositories','settings'].includes(view)) view='apps';
    let leaf='';try{leaf=decodeURIComponent(parts[2] || '');}catch{}
    const ctx={view,sub:parts[1] || (view==='benchmarks'?'spectra':view==='settings'?'registration':'overview'),leaf,generation:++ui.generation};ui.current=ctx;
    for(const link of document.querySelectorAll('[data-view]')) {if(link.dataset.view===view)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');}
    const label={apps:'Apps',updates:'Updates',benchmarks:'Benchmarks',repositories:'Repositories',settings:'Settings'}[view];document.title=label+' – R4OS Server';content.setAttribute('aria-label','Workspace: '+label);
    document.querySelector('.submenu-column').innerHTML='';ui.context(ctx);
    ui.show(ctx,'<div class="loading-state" role="status">Loading data …</div>');
    try {
      if(!ui.views[view]){ui.page(ctx,label,'',ui.empty('This area is being prepared.'));return;}
      await ui.views[view](ctx);
    } catch(error) {
      if(ctx.generation!==ui.generation)return;
      ui.page(ctx,label,'',ui.empty('Data unavailable',error.message));ui.status(error.message,true);
    }
  };
  let checking=false,connected=false;
  const connectionError='Connection to the WebApp was interrupted. Retrying automatically.';
  ui.check = async () => {
    if(checking)return;checking=true;
    try {
      const value=await ui.api('status');
      if(value.status!=='ok')throw new Error();
      document.querySelector('#connection').dataset.state='connected';document.querySelector('#connection-label').textContent='Connected';document.querySelector('#version').textContent='v'+value.version;
      const message=document.querySelector('#status-message');
      if(!connected && (!message.classList.contains('is-error')||message.textContent===connectionError))ui.status('Connected.');connected=true;
    } catch {connected=false;document.querySelector('#connection').dataset.state='error';document.querySelector('#connection-label').textContent='Disconnected';ui.status(connectionError,true);}
    finally {checking=false;}
  };
  document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-action]');if(!button)return;
    if(button.dataset.action==='refresh')ui.navigate();
    if(button.dataset.action==='close-dialog')document.querySelector('#detail-dialog')?.close();
  });
  window.addEventListener('hashchange',ui.navigate);
  window.addEventListener('online',ui.check);
  document.addEventListener('DOMContentLoaded',()=>{ui.navigate();ui.check();setInterval(ui.check,15000);});
  return ui;
})();
