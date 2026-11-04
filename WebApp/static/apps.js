"use strict";
(() => {
  const U=window.R4,E=U.esc,api=(path,options)=>U.api('apps/'+path,options);
  let editing=null,lastValue=null,overviewRequest=0,overviewGeneration=0;
  const pending=new Map(),errors=new Map();
  const active=job=>job&&['queued','running'].includes(job.status);
  const labels={not_downloaded:'Not downloaded',current:'Up to date',outdated:'Update available',local_available:'Available locally',local_newer:'Local version retained',damaged:'Local files damaged',conflict:'Release changed'};
  U.views.apps=async ctx=>{U.side(ctx,[['overview','Overview']]);return loadOverview(ctx);};
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
  function entry(app,value){
    const release=app.release||{},job=release.job||app.refresh_job;
    const related=(value.active_jobs||[]).find(item=>U.repositoryActions.includes(item.kind)&&U.affectsRepository(item,{name:app.repository,connection_id:app.connection_id}));
    const own=active(job)?job:related;
    const refreshing=own?.kind==='refresh'||own?.kind==='app_refresh';
    const feedback=U.entryRefresh.state(app.repository,refreshing||job?.kind==='app_refresh'||job?.kind==='refresh'?job:app.refresh_job);
    const action=pending.get(app.repository)||(own?.kind?.startsWith('app_')?own.kind.slice(4):refreshing?'refresh':'');
    const busy=pending.has(app.repository)||!!own||feedback.busy;
    const error=errors.get(app.repository)||(!busy&&['failed','interrupted'].includes(job?.status)?job.error:'')||(!busy?release.error||(!app.source_available?app.source_error:''):'');
    return {release,job:own||job,feedback,action,busy,error};
  }
  function button(action,name,label,disabled=false,title='',loading=false){
    return '<span class="action-help" title="'+E(title)+'"'+(disabled?' tabindex="0" role="group" aria-label="'+E(title||label)+'"':'')+'><button type="button" id="app-action-'+E(action+'-'+(name||'all'))+'" class="button subtle'+(loading?' is-loading':'')+'" data-app-action="'+E(action)+'"'+(name?' data-app-name="'+E(name)+'"':'')+(disabled?' disabled':'')+(loading?' aria-busy="true"':'')+'>'+(action.startsWith('refresh')?U.refreshIcon:'')+E(label)+'</button></span>';
  }
  function renderOverview(ctx,value,partial=false){
    const states=new Map(value.apps.map(app=>[app.repository,entry(app,value)]));
    const rows=value.apps.map(app=>{
      const {release,job,feedback,action,busy,error}=states.get(app.repository),name=app.repository;
      const checking=busy&&action==='refresh';
      const refreshLabel=checking?'Refreshing…':feedback.updated?'Updated':'Refresh';
      const reason=!app.repository_available?app.repository_error:busy?'Wait for this app’s current action to finish.':release.reason||'';
      const refresh=button('refresh',name,refreshLabel,busy||!app.repository_available,reason||'Check this app, its source and the latest GitHub release.',checking);
      const transfer=release.local?'update':'download',transferring=busy&&['download','update'].includes(action);
      const transferLabel=transferring?(action==='update'?'Updating…':'Downloading…'):(transfer==='update'?'Update':'Download');
      const status=checking?'Checking':transferring?'Downloading':busy&&action==='remove_local'?'Removing local files':!release.local&&release.remote_status==='unknown'?'Not checked':!release.local&&release.remote_status==='missing'?'No release':labels[release.state]||'Not downloaded';
      const versions=release.local?'Version '+release.local.version:release.available?'Version '+release.available.version:'Release version unavailable';
      const newer=release.local&&release.available&&release.local.version!==release.available.version?'<small class="cell-subline app-available-version">Available '+E(release.available.version)+'</small>':'';
      const progress=busy&&job?.result?.progress,percent=Number(progress?.percent),known=progress?.percent!==null&&progress?.percent!==undefined&&Number.isFinite(percent);
      const releaseLink=release.available?.url||release.local?.url;
      const content='<div class="record-grid app-overview-grid"><div class="record-identity"><h2 class="record-name app-name">'+E(app.display_name)+'</h2><span class="record-subline app-version">'+E(versions)+'</span>'+newer+
        '<div class="app-release-status"><span class="badge app-release-badge" data-release-state="'+E(release.state||'unknown')+'">'+E(status)+'</span></div>'+
        (app.description?'<p class="app-description">'+E(app.description)+'</p>':'')+
        (progress?'<progress class="app-progress" max="100"'+(known?' value="'+Math.min(100,Math.max(0,percent))+'"':'')+' aria-label="App download progress"></progress>':'')+'</div>'+
        '<div role="group" aria-label="Install-Path"><span class="app-install-path">'+E(app.install_path)+'</span></div>'+
        '<div role="group" aria-label="Source">'+E(app.owner)+'<small class="cell-subline">'+E(app.repository_name)+'</small></div><span class="badge app-visibility" title="Source repository visibility">'+(app.private?'Private':'Public')+'</span></div>';
      const actions=refresh+button(transfer,name,transferLabel,busy||!release['can_'+transfer],reason||(transfer==='update'?'Download the newer published release.':'Download the published GitHub release.'),transferring)+
        button('remove_local',name,busy&&action==='remove_local'?'Removing…':'Remove local files',busy||!release.can_remove,busy?reason:release.can_remove?'Remove this app’s downloaded releases.':'No local app files are available.',busy&&action==='remove_local')+
        (releaseLink?'<a class="button subtle" href="'+E(releaseLink)+'" target="_blank" rel="noopener noreferrer">Release</a>':'')+
        '<button type="button" id="app-edit-'+E(name)+'" class="button subtle" data-edit-app="'+E(name)+'" aria-label="Edit '+E(app.display_name)+'">Edit</button>'+
        (error?'<div class="entry-refresh-message" role="alert"><span>'+E(error)+'</span>'+(!errors.has(name)&&job?.id?'<button type="button" class="button subtle" data-repo-action="job" data-name="'+E(job.id)+'">Log</button>':'')+'</div>':'');
      return U.record(content,actions,' data-record-key="'+E(name)+'" data-app-repository="'+E(name)+'" aria-busy="'+busy+'"',busy?'is-refreshing':'');
    });
    const bulkBusy=pending.has('*'),downloadCount=value.apps.filter(app=>!states.get(app.repository).busy&&app.release?.can_download).length,updateCount=value.apps.filter(app=>!states.get(app.repository).busy&&app.release?.can_update).length;
    const toolbar=button('download_all','',bulkBusy&&pending.get('*')==='download_all'?'Starting…':'Download all',bulkBusy||downloadCount===0,downloadCount?'Download all apps without a local release.':'No apps are ready to download. Refresh to check published releases.')+
      button('update_all','',bulkBusy&&pending.get('*')==='update_all'?'Starting…':'Update all ('+updateCount+')',bulkBusy||updateCount===0,updateCount?'Download all newer app releases.':'No checked apps have a newer release.');
    const html='<section id="apps-overview" class="apps-list" aria-label="Apps overview">'+(errors.has('*')?'<p class="workspace-notice" role="alert">'+E(errors.get('*'))+'</p>':'')+(rows.length?U.records(rows,'Apps'):value.publishing?U.empty('Adding apps','Apps appear here after their manifests have been read.'):U.empty('No apps listed','Enable Show in Apps for an application in Repositories → Overview.'))+'</section>';
    U.page(ctx,'Apps','',html,toolbar,button('refresh_all','',bulkBusy&&pending.get('*')==='refresh_all'?'Starting…':'Refresh',bulkBusy||value.apps.length===0,'Check all listed apps for published releases.'),partial);
    U.status('Apps loaded.');
    if(value.publishing||pending.size||[...states.values()].some(item=>item.busy))poll(ctx);
  }
  function poll(ctx){if(U.poll)return;U.poll=setTimeout(()=>{U.poll=null;if(ctx.generation===U.generation)loadOverview(ctx,true).catch(error=>U.status(error.message,true));},1500);}
  function redraw(){const ctx=U.current;if(ctx?.view==='apps'&&overviewGeneration===ctx.generation&&lastValue)renderOverview(ctx,lastValue,true);}
  async function start(action,name){
    const key=name||'*';if(pending.has(key))return;
    pending.set(key,action);errors.delete(key);redraw();
    try{
      const value=await api('actions/'+action,{method:'POST',body:name?{name}:{}});
      if(value.skipped?.length)errors.set(key,value.skipped.map(item=>item.error).join(' '));
      if(lastValue){
        const jobs=value.jobs||[value];
        for(const job of jobs){
          const app=lastValue.apps.find(item=>item.repository===job.subject);if(app?.release)app.release.job=job;
          lastValue.active_jobs=[...(lastValue.active_jobs||[]).filter(item=>item.id!==job.id),job];
        }
      }
    }catch(error){errors.set(key,error.message);U.status(error.message,true);}
    finally{
      pending.delete(key);redraw();const ctx=U.current;
      if(ctx?.view==='apps')loadOverview(ctx,true).catch(error=>U.status(error.message,true));
    }
  }
  window.addEventListener('jobs-updated',()=>{const ctx=U.current;if(ctx?.view==='apps'&&overviewGeneration===ctx.generation&&!U.poll)loadOverview(ctx,true).catch(error=>U.status(error.message,true));});
  window.addEventListener('entry-refresh',event=>{
    const ctx=U.current;if(ctx?.view!=='apps')return;
    const {name,phase,job}=event.detail;
    if(lastValue&&overviewGeneration===ctx.generation&&job){
      const app=lastValue.apps.find(item=>item.repository===name);if(app){app.refresh_job=job;if(app.release)app.release.job=job;}
      lastValue.active_jobs=[...(lastValue.active_jobs||[]).filter(item=>item.id!==job.id),job].filter(item=>active(item));
    }
    redraw();if(phase==='submitted'||phase==='error')loadOverview(ctx,true).catch(error=>U.status(error.message,true));
  });
  document.addEventListener('click',async event=>{
    const action=event.target.closest('[data-app-action]');
    if(action&&!action.disabled){await start(action.dataset.appAction,action.dataset.appName);return;}
    const button=event.target.closest('[data-edit-app]');if(!button)return;
    const generation=U.generation;button.disabled=true;
    try{
      const app=await api('items/'+encodeURIComponent(button.dataset.editApp));if(generation!==U.generation)return;
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
    const app=editing,generation=U.generation,submit=form.querySelector('[type=submit]');
    const display=form.elements.namedItem('display_name').value.trim(),path=form.elements.namedItem('install_path').value.trim();
    const body={display_name:!display||(display===app.display_name&&app.display_name_override===null)?null:display,
      install_path:!path||(path===app.install_path&&app.install_path_override===null)?null:path,description:form.elements.namedItem('description').value};
    submit.disabled=true;form.querySelector('.form-message').textContent='';
    try{
      await api('items/'+encodeURIComponent(form.dataset.repository),{method:'PUT',body});
      if(generation===U.generation&&form.isConnected){await loadOverview(U.current,true);if(form.isConnected)form.closest('dialog').close();U.status('App settings saved.');}
    }catch(error){if(form.isConnected)form.querySelector('.form-message').textContent=error.message;U.status(error.message,true);}
    finally{if(submit.isConnected)submit.disabled=false;}
  });
})();
