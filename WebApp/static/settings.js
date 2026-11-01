"use strict";
(() => {
  const U=window.R4,E=U.esc;
  const events={authenticated:'Authenticated',blocked:'Source blocked',unblocked:'Source unblocked',block_expired:'Block expired',setting_changed:'Setting changed',revoked:'Registration revoked',registration_required:'Registration required'};
  U.views.settings=async ctx=>{
    U.side(ctx,[['registration','Registration'],['limits','Service limits'],['audit','Access log']]);
    const state=await U.api('settings/state');if(ctx.generation!==U.generation)return;
    let body='',title='Registration',description='Shared client access for software, updates and benchmarks.';
    if(ctx.sub==='registration'){
      body='<div class="metric-grid">'+U.card('Registered sources',U.count(state.allowed.length))+U.card('Blocked sources',U.count(state.blocked.length))+U.card('LAN address',state.network.host)+'</div>';
      body+='<section class="panel"><div class="panel-heading"><h2>Registered clients</h2></div>'+(state.allowed.length?U.table(['IP address','Registered','Last access','Action'],state.allowed.map(item=>[E(item.ip),E(U.date(item.authenticated_at)),E(U.date(item.last_seen_at)),'<button class="button subtle" data-registration-action="revoke" data-ip="'+E(item.ip)+'">Revoke</button>'])):U.empty('No registered clients','Clients sign in through the shared registration service.'))+'</section>';
      body+='<section class="panel"><div class="panel-heading"><h2>Blocked sources</h2></div>'+(state.blocked.length?U.table(['IP address','Reason','Blocked until','Action'],state.blocked.map(item=>[E(item.ip),E(item.reason),E(U.date(item.expires_at)),'<button class="button subtle" data-registration-action="unblock" data-ip="'+E(item.ip)+'">Unblock</button>'])):U.empty('No active blocks'))+'</section>';
      body+='<section class="panel"><div class="panel-heading"><h2>Block source</h2></div><form id="block-source" class="panel-body"><div class="form-grid"><label>IP address<input name="ip" maxlength="45" required placeholder="e.g. 10.0.0.10"></label><label>Block duration<select name="duration"><option value="3600">One hour</option><option value="86400">One day</option><option value="604800">Seven days</option></select></label></div><p class="muted">A block applies to client access across all data services. Revoked registrations must sign in again.</p><button class="button danger" type="submit">Block source</button><p class="form-message" role="alert"></p></form></section>';
    }else if(ctx.sub==='limits'){
      title='Service limits';description='Per-source limits apply to each service; blocks are shared.';
      body='<section class="panel"><div class="panel-heading"><h2>Requests per second and IP</h2></div><form id="service-rates" class="panel-body"><div class="form-grid">'+Object.entries(state.rates).map(([name,value])=>'<label>'+E(({updates:'Updates',benchmarks:'Benchmarks',software:'Software',registration:'Registration'})[name])+'<input name="'+E(name)+'" type="number" min="1" max="10000" step="1" required value="'+E(value)+'"></label>').join('')+'</div><div class="form-actions"><button class="button primary" type="submit">Save limits</button></div><p class="form-message" role="alert"></p></form></section>';
      body+=U.fold('Ports and network',state.network);
    }else{
      title='Access log';description='Shared registration, blocks and service events.';
      body='<section class="panel"><div class="panel-heading"><h2>Recent events</h2></div>'+(state.audit.length?U.table(['Time','Service','Event','Source','Details'],state.audit.map(item=>[E(U.date(item.created_at)),E(({registration:'Registration',updates:'Updates',benchmarks:'Benchmarks',software:'Software'})[item.service]||item.service),E(events[item.event]||item.event),E(item.ip || '—'),E(item.note)])):U.empty('No access events yet'))+'</section>';
    }
    U.page(ctx,title,description,body);U.status('Settings loaded.');
  };
  document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-registration-action]');if(!button)return;button.disabled=true;
    try{await U.api('settings/'+button.dataset.registrationAction,{method:'POST',body:{ip:button.dataset.ip}});await U.navigate();U.status(button.dataset.registrationAction==='unblock'?'Source unblocked.':'Registration revoked.');}
    catch(error){U.status(error.message,true);button.disabled=false;}
  });
  document.addEventListener('submit',async event=>{
    const form=event.target;if(!['block-source','service-rates'].includes(form.id))return;event.preventDefault();
    const button=form.querySelector('[type="submit"]');button.disabled=true;
    try{
      if(form.id==='block-source')await U.api('settings/block',{method:'POST',body:{ip:form.elements.ip.value.trim(),duration_seconds:Number(form.elements.duration.value)}});
      else await U.api('settings/rates',{method:'PUT',body:Object.fromEntries([...new FormData(form)].map(([name,value])=>[name,Number(value)]))});
      await U.navigate();U.status(form.id==='block-source'?'Source blocked.':'Service limits saved.');
    }catch(error){form.querySelector('.form-message').textContent=error.message;U.status(error.message,true);button.disabled=false;}
  });
})();
