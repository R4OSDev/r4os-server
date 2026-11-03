"use strict";
(() => {
  const U=window.R4,E=U.esc,api=(path,options)=>U.api('repositories/'+path,options);
  const active=job=>['queued','running'].includes(job.status);
  let current=null;
  const pencil='<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="m15 4 5 5M4 20l5-1 12-12-5-5L4 14v6Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  async function refresh(){
    if(U.current?.view!=='repositories'||U.current.sub!=='connections')return;
    const leaf=U.current.leaf,scroll=U.scrollContainer().scrollTop;
    await U.navigate();
    if(U.current.view==='repositories'&&U.current.sub==='connections'&&U.current.leaf===leaf)U.scrollContainer().scrollTop=scroll;
  }
  function schedule(ctx){
    U.poll=setTimeout(()=>{
      if(ctx.generation!==U.generation)return;
      if(document.querySelector('#detail-dialog[open]'))schedule(ctx);
      else refresh().catch(error=>U.status(error.message,true));
    },2500);
  }
  U.connectionsView=async ctx=>{
    const state=await api('state');if(ctx.generation!==U.generation)return;
    const items=state.connections,key=items.some(item=>item.id===ctx.leaf)?ctx.leaf:'main';
    current=items.find(item=>item.id===key);
    if(!current)return;
    const value=await api('connections/'+encodeURIComponent(key));if(ctx.generation!==U.generation)return;
    const connection=value.connection,repos=value.repositories,ready=connection.status==='ready';
    const column=document.querySelector('.third-column');
    column.innerHTML='<button type="button" class="button subtle connection-create" data-connection-action="new">+ New Connection</button><nav class="secondary-navigation" aria-label="GitHub connections">'+items.map(item=>'<div class="connection-row"><a class="sub-item" href="#repositories/connections/'+E(item.id)+'"'+(item.id===key?' aria-current="page"':'')+'>'+E(item.name)+'<small>'+E(item.owner)+'</small></a><button type="button" class="connection-edit" data-connection-action="edit" data-id="'+E(item.id)+'" title="Edit '+E(item.name)+'" aria-label="Edit '+E(item.name)+'">'+pencil+'</button></div>').join('')+'</nav>';
    let html='<div class="metric-grid">'+U.card('Repositories',U.count(repos.filter(repo=>repo.available).length))+U.card('Enabled',U.count(repos.filter(repo=>repo.enabled).length))+U.card('Access',connection.has_token===null?'Token status unavailable':connection.has_token?'Token stored':'Public')+'</div>';
    html+='<p><a class="artifact-link" href="'+E(connection.url)+'" target="_blank" rel="noopener noreferrer">'+E(connection.url)+'</a></p>';
    html+='<p class="muted">Local folder: <code>'+E(state.project_root+'/Repositories/'+connection.directory+'/')+'</code></p>';
    html+='<p class="muted">Choose which repositories are available in the server. New private repositories are disabled by default. Downloads are managed in Overview; disabling keeps the local checkout.</p>';
    if(!ready)html+='<p class="workspace-notice" role="status">'+E(connection.status==='delete_failed'?'Removal was not completed. Edit this connection to retry.':'Connection removal is queued or in progress. Its local repositories will be deleted after current work finishes.')+'</p>';
    if(connection.credential_error)html+='<p class="workspace-notice" role="alert">'+E(connection.credential_error)+'</p>';
    if(connection.error||connection.catalog.error)html+='<p class="workspace-notice" role="alert">'+E(connection.error||connection.catalog.error)+'</p>';
    if(!state.worker.ready)html+='<p class="workspace-notice">The repository worker is unavailable. Jobs remain queued until it starts.</p>';
    html+='<section class="record-section"><div class="panel-heading"><h2>Repositories</h2><div class="form-actions"><button type="button" class="button subtle" data-connection-action="enable-all"'+(!ready||!repos.some(repo=>repo.available)?' disabled':'')+'>Enable all</button><button type="button" class="button subtle" data-connection-action="disable-all"'+(!ready||!repos.length?' disabled':'')+'>Disable all</button></div></div>';
    html+=repos.length?U.records(repos.map(repo=>U.record('<div class="record-head"><div><a class="record-name artifact-link" href="'+E(repo.url)+'" target="_blank" rel="noopener noreferrer">'+E(repo.display_name)+'</a><p class="record-description">'+E(repo.description)+'</p>'+(!repo.available?'<p class="muted">'+E(repo.unavailable_reason)+'</p>':'')+'</div><span class="badge">'+(repo.private?'Private':'Public')+'</span></div><span class="record-subline">'+E(repo.local.commit||repo.local.status==='empty'?'Downloaded':'Not downloaded')+'</span>','<label class="toggle-control"><input type="checkbox" data-connection-repository="'+E(repo.name)+'" aria-label="Use '+E(repo.display_name)+' in the server"'+(repo.enabled?' checked':'')+(!ready||(!repo.available&&!repo.enabled)?' disabled':'')+'> Use in server</label>')),'Connection repositories'):U.empty('No repositories yet',connection.catalog.updated?'This account has no accessible repositories.':'Refresh the catalogue to load this account’s repositories.');
    html+='</section>';
    const jobs=state.jobs.filter(job=>active(job)&&(job.payload.connection_id===key||repos.some(repo=>repo.name===job.subject)));
    if(jobs.length)html+='<p class="muted" role="status">'+jobs.map(job=>E(job.phase||'Job queued')).join(' · ')+'</p>';
    U.page(ctx,connection.name,'',html,'<button type="button" class="button subtle" data-connection-action="catalog"'+(!ready?' disabled':'')+'>Refresh catalogue</button>');
    U.status('Connections loaded.');
    if(jobs.length||!ready)schedule(ctx);
  };
  async function editor(key){
    const connection=key?(await api('connections/'+encodeURIComponent(key))).connection:null;
    const ready=!connection||connection.status==='ready',stored=connection&&connection.has_token!==false;
    U.open(connection?'Edit '+connection.name:'New connection','<form id="connection-edit" class="stack-form" data-id="'+E(connection?.id||'')+'" data-revision="'+E(connection?.revision||'')+'"><label>Name<input name="name" maxlength="80" value="'+E(connection?.name||'')+'"'+(!ready?' disabled':'')+'></label><label>GitHub address<input name="url" type="url" required placeholder="https://github.com/account" value="'+E(connection?.url||'')+'"'+(!ready?' disabled':'')+'></label><label>Account type<select name="account_type"'+(connection?.is_main||!ready?' disabled':'')+'><option value="user"'+(connection?.account_type==='user'?' selected':'')+'>Account</option><option value="organisation"'+(connection?.account_type==='organisation'?' selected':'')+'>Organisation</option></select></label>'+(connection?.is_main?'<p class="muted">R4OS Organisation is the fixed main connection. Its address and token can be changed.</p>':'')+'<label>Token (optional)<input name="token" type="password" autocomplete="new-password" spellcheck="false" maxlength="4096" placeholder="'+(stored?'Leave blank to keep the saved token':'Public access without a token')+'"'+(!ready?' disabled':'')+'></label><p class="muted">'+(stored?(connection.has_token===null?'The saved token status is unavailable. ':'A token is stored. '):'')+'Private sources need Contents: read permission. Write access depends on the token’s actual permissions for each repository. Artefact uploads will be added later.</p>'+(stored?'<label class="toggle-control"><input name="remove_token" type="checkbox"'+(!ready?' disabled':'')+'> Remove saved token</label>':'')+(connection?.error||connection?.credential_error?'<p class="workspace-notice">'+E(connection.error||connection.credential_error)+'</p>':'')+'<div class="form-actions">'+(ready?'<button class="button primary" type="submit">Save connection</button>':'')+(connection&&!connection.is_main?'<button class="button danger" type="button" data-connection-action="delete-preview" data-id="'+E(connection.id)+'">'+(ready?'Delete connection':'Retry removal')+'</button>':'')+'</div><p class="form-message" role="alert"></p></form>');
  }
  async function deletion(key){
    const value=await api('connections/'+encodeURIComponent(key)+'/delete-preview');
    const repos=value.repositories.filter(repo=>repo.local);
    U.open('Delete '+value.connection.name,'<form id="connection-delete" data-id="'+E(key)+'" class="stack-form"><p>This permanently removes the connection, its saved token and all '+U.count(repos.length)+' local repository checkouts. Uncommitted local changes will be lost.</p>'+(repos.length?'<ul>'+repos.map(repo=>'<li>'+E(repo.display_name)+(repo.modified?' · Local changes':'')+'</li>').join('')+'</ul>':'')+'<p class="muted">Queued work for this connection is cancelled. Running work finishes before removal. Existing build and release artefacts are kept.</p><label class="toggle-control"><input name="confirmed" type="checkbox" required> Delete this connection and its local repositories</label><div class="form-actions"><button type="submit" class="button danger">Delete connection</button><button type="button" class="button subtle" data-action="close-dialog">Cancel</button></div><p class="form-message" role="alert"></p></form>');
  }
  document.addEventListener('click',async event=>{
    const node=event.target.closest('[data-connection-action]');if(!node)return;
    const action=node.dataset.connectionAction;node.disabled=true;
    try{
      if(action==='new')await editor();
      else if(action==='edit')await editor(node.dataset.id);
      else if(action==='delete-preview')await deletion(node.dataset.id);
      else if(action==='catalog'){
        await api('actions/catalog',{method:'POST',body:{connection_id:current.id}});await refresh();U.status('Catalogue refresh queued.');
      }else{
        await api('connections/'+current.id+'/selection',{method:'PUT',body:{enabled:action==='enable-all'}});
        await refresh();U.status('Repository selection saved.');
      }
    }catch(error){U.status(error.message,true);}finally{node.disabled=false;}
  });
  document.addEventListener('change',async event=>{
    const node=event.target;if(!node.matches('[data-connection-repository]'))return;
    const enabled=node.checked,id=node.dataset.connectionRepository;node.disabled=true;
    try{
      await api('connections/'+current.id+'/selection',{method:'PUT',body:{repositories:[id],enabled}});
      await refresh();document.querySelector('[data-connection-repository="'+CSS.escape(id)+'"]')?.focus();
      U.status('Repository selection saved.');
    }catch(error){node.checked=!enabled;node.disabled=false;U.status(error.message,true);}
  });
  document.addEventListener('submit',async event=>{
    const form=event.target;if(!['connection-edit','connection-delete'].includes(form.id))return;
    event.preventDefault();const node=form.querySelector('[type=submit]');node.disabled=true;
    try{
      const values=new FormData(form),key=form.dataset.id;
      if(form.id==='connection-delete'){
        await api('connections/'+key,{method:'DELETE',body:{confirmed:values.has('confirmed')}});
        await refresh();U.status('Connection removal queued.');
      }else{
        const body={name:values.get('name'),url:values.get('url'),account_type:values.get('account_type')||'organisation',revision:Number(form.dataset.revision),remove_token:values.has('remove_token')};
        const credential=String(values.get('token')||'');if(credential)body.token=credential;
        const saved=await api('connections'+(key?'/'+key:''),{method:key?'PUT':'POST',body});
        form.elements.token.value='';
        const hash='#repositories/connections/'+saved.id;
        if(location.hash===hash)await refresh();else location.hash=hash;
        U.status('Connection saved. Catalogue refresh queued.');
      }
    }catch(error){form.querySelector('.form-message').textContent=error.message;U.status(error.message,true);}finally{node.disabled=false;}
  });
})();
