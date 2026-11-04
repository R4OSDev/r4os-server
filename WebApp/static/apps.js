"use strict";
(() => {
  const U=window.R4,E=U.esc,api=(path,options)=>U.api('apps/'+path,options);
  let editing=null,lastValue=null,overviewRequest=0,overviewGeneration=0;
  U.views.apps=async ctx=>{
    U.side(ctx,[['overview','Overview']]);
    return loadOverview(ctx);
  };
  async function loadOverview(ctx,partial=false){
    if(ctx.generation!==U.generation)return;
    const request=++overviewRequest;clearTimeout(U.poll);U.poll=null;
    try{
      const value=await api('overview');if(ctx.generation!==U.generation||request!==overviewRequest)return;
      lastValue=value;overviewGeneration=ctx.generation;renderOverview(ctx,value,partial);
    }catch(error){
      if(ctx.generation!==U.generation||request!==overviewRequest)return;
      if(!partial||overviewGeneration!==ctx.generation)throw error;
      U.status('Could not refresh app status. Retrying: '+error.message,true);poll(ctx);
    }
  }
  function renderOverview(ctx,value,partial=false){
    const jobs=value.active_jobs||[];
    const rows=value.apps.map(app=>{
      const feedback=U.entryRefresh.state(app.repository,app.refresh_job),repo={name:app.repository,connection_id:app.connection_id};
      const blocked=jobs.some(job=>U.repositoryActions.includes(job.kind)&&U.affectsRepository(job,repo));
      const error=feedback.error||(!feedback.busy&&!app.source_available?app.source_error:'');
      const label=feedback.busy?'Refreshing…':error?'Retry':feedback.updated?'Updated':'Refresh';
      const refresh='<button type="button" class="button subtle'+(feedback.busy?' is-loading':'')+(label==='Updated'?' is-updated':'')+'" data-app-refresh="'+E(app.repository)+'" aria-label="Refresh '+E(app.display_name+' ('+app.owner+'/'+app.repository_name+')')+'" title="'+E(!app.repository_available?app.repository_error||app.source_error:blocked&&!feedback.busy?'Wait for the current repository action to finish.':'Refresh this app and its repository metadata.')+'"'+(feedback.busy||blocked||!app.repository_available?' disabled':'')+(feedback.busy?' aria-busy="true"':'')+'>'+(label==='Updated'?U.updatedIcon:U.refreshIcon)+E(label)+'</button>';
      return U.record(
      '<div class="record-grid app-overview-grid"><div class="record-identity"><h2 class="record-name app-name">'+E(app.display_name)+'</h2><span class="record-subline app-version">Version '+E(app.version)+'</span>'+(app.description?'<p class="app-description">'+E(app.description)+'</p>':'')+'</div>'+
      '<div role="group" aria-label="Install-Path"><span class="app-install-path">'+E(app.install_path)+'</span></div>'+
      '<div role="group" aria-label="Source">'+E(app.owner)+'<small class="cell-subline">'+E(app.repository_name)+'</small>'+(!app.source_available?'<p class="muted">'+E(app.source_error||'Source unavailable')+'</p>':'')+'</div><span class="badge app-visibility" title="Source repository visibility">'+(app.private?'Private':'Public')+'</span></div>',
      refresh+'<button type="button" class="button subtle" data-edit-app="'+E(app.repository)+'" aria-label="Edit '+E(app.display_name)+'">Edit</button>'+(error?'<div class="entry-refresh-message" role="alert"><span>'+E('Refresh failed: '+error)+'</span>'+(feedback.log?'<button type="button" class="button subtle" data-repo-action="job" data-name="'+E(feedback.log)+'">Log</button>':'')+'</div>':''),
      ' data-record-key="'+E(app.repository)+'" data-app-repository="'+E(app.repository)+'" aria-busy="'+feedback.busy+'"',
      feedback.busy?'is-refreshing':''
    );});
    const html='<section id="apps-overview" class="apps-list" aria-label="Apps overview">'+(rows.length?U.records(rows,'Apps'):value.publishing?U.empty('Adding apps','Apps appear here after their manifests have been read.'):U.empty('No apps listed','Enable Show in Apps for an application in Repositories → Overview.'))+'</section>';
    U.page(ctx,'Apps','',html,'',true,partial);U.status('Apps loaded.');
    if(value.publishing||value.apps.some(app=>U.entryRefresh.state(app.repository,app.refresh_job).busy||jobs.some(job=>U.affectsRepository(job,{name:app.repository,connection_id:app.connection_id}))))poll(ctx);
  }
  function poll(ctx){if(U.poll)return;U.poll=setTimeout(()=>{U.poll=null;if(ctx.generation===U.generation)loadOverview(ctx,true).catch(error=>U.status(error.message,true));},1500);}
  window.addEventListener('jobs-updated',()=>{
    const ctx=U.current;if(ctx?.view==='apps'&&overviewGeneration===ctx.generation&&!U.poll)loadOverview(ctx,true).catch(error=>U.status(error.message,true));
  });
  window.addEventListener('entry-refresh',event=>{
    const ctx=U.current;if(ctx?.view!=='apps')return;
    const {name,phase,job}=event.detail;
    if(lastValue&&overviewGeneration===ctx.generation){
      if(job){
        const app=lastValue.apps.find(item=>item.repository===name);if(app)app.refresh_job=job;
        lastValue.active_jobs=[...(lastValue.active_jobs||[]).filter(item=>item.id!==job.id),job].filter(item=>['queued','running'].includes(item.status));
      }
      renderOverview(ctx,lastValue,true);
    }
    if(phase==='submitted'||phase==='error')loadOverview(ctx,true).catch(error=>U.status(error.message,true));
  });
  document.addEventListener('click',async event=>{
    const refresh=event.target.closest('[data-app-refresh]');
    if(refresh){await U.entryRefresh.request(refresh.dataset.appRefresh);return;}
    const button=event.target.closest('[data-edit-app]');if(!button)return;
    const generation=U.generation;button.disabled=true;
    try{
      const app=await api('items/'+encodeURIComponent(button.dataset.editApp));
      if(generation!==U.generation)return;
      editing=app;
      U.open('Edit '+app.display_name,'<form id="app-settings" class="stack-form" data-repository="'+E(app.repository)+'"><p class="muted">'+E(app.owner)+' / '+E(app.repository_name)+'</p>'+
        '<label>Display-Name<input name="display_name" maxlength="128" value="'+E(app.display_name)+'" placeholder="'+E(app.default_display_name)+'"><small class="muted">Default: '+E(app.default_display_name)+'. Clear this field to use the manifest name.</small></label>'+
        '<label>Install-Path<input name="install_path" maxlength="240" spellcheck="false" value="'+E(app.install_path)+'" placeholder="'+E(app.default_install_path)+'"><small class="muted">Default: '+E(app.default_install_path)+'. Clear this field to use the default path.</small></label>'+
        '<label>Description<textarea name="description" rows="6" maxlength="8192">'+E(app.description)+'</textarea></label>'+
        '<div class="form-actions"><button type="submit" class="button primary">Save</button><button type="button" class="button subtle" data-action="close-dialog">Cancel</button></div><p class="form-message" role="alert"></p></form>');
    }catch(error){U.status(error.message,true);}finally{if(button.isConnected)button.disabled=false;}
  });
  document.addEventListener('submit',async event=>{
    const form=event.target;if(form.id!=='app-settings')return;event.preventDefault();
    const app=editing,generation=U.generation,submit=form.querySelector('[type="submit"]');
    const display=form.elements.namedItem('display_name').value.trim(),path=form.elements.namedItem('install_path').value.trim();
    const body={display_name:!display||(display===app.display_name&&app.display_name_override===null)?null:display,
      install_path:!path||(path===app.install_path&&app.install_path_override===null)?null:path,
      description:form.elements.namedItem('description').value};
    submit.disabled=true;form.querySelector('.form-message').textContent='';
    try{
      await api('items/'+encodeURIComponent(form.dataset.repository),{method:'PUT',body});
      if(generation===U.generation&&form.isConnected){
        await loadOverview(U.current,true);
        if(form.isConnected)form.closest('dialog').close();
        U.status('App settings saved.');
      }
    }catch(error){if(form.isConnected)form.querySelector('.form-message').textContent=error.message;U.status(error.message,true);}
    finally{if(submit.isConnected)submit.disabled=false;}
  });
})();
