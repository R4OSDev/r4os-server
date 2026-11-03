"use strict";
(() => {
  const U=window.R4,E=U.esc,api=(path,options)=>U.api('repositories/'+path,options),sdk=(path,options)=>U.api('sdk/'+path,options);
  const states={missing:'Not downloaded',current:'Up to date',outdated:'Update available',modified:'Locally modified',diverged:'Different branch',ahead:'Local commits',unknown:'Status unknown',empty:'Empty repository',conflict:'Local conflict',queued:'Queued',running:'Running',completed:'Completed',failed:'Failed',cancelled:'Cancelled',interrupted:'Interrupted'};
  let filter='',category='',account='',lastState=null,dialogJob=null,dialogOffset=0,dialogText='',dialogGeneration=0,dialogTrigger=null;
  const active=job=>['queued','running'].includes(job.status);
  const status=value=>'<span class="badge">'+E(states[value]||value||'Unknown')+'</span>';
  const button=(label,action,name='',disabled=false,primary=false,title='')=>'<button type="button" class="button '+(primary?'primary':'subtle')+'" data-repo-action="'+E(action)+'" data-name="'+E(name)+'"'+(disabled?' disabled':'')+(title?' title="'+E(title)+'"':'')+'>'+E(label)+'</button>';
  async function refreshPage(ctx){
    const focused=document.activeElement,content=document.querySelector('#content-area'),scroll=content?.scrollTop||0;
    let selector=focused?.id?'#'+CSS.escape(focused.id):focused?.dataset.repoAction?'[data-repo-action="'+CSS.escape(focused.dataset.repoAction)+'"][data-name="'+CSS.escape(focused.dataset.name||'')+'"]':null;
    if(!selector&&focused?.closest('form')?.id&&focused.type==='submit')selector='#'+CSS.escape(focused.closest('form').id)+' [type="submit"]';
    const start=focused?.selectionStart,end=focused?.selectionEnd;
    await U.navigate();
    if(U.current.view!==ctx.view||U.current.sub!==ctx.sub||U.current.leaf!==ctx.leaf)return;
    const replacement=selector?document.querySelector(selector):null;
    replacement?.focus({preventScroll:true});
    if(typeof start==='number'&&replacement?.setSelectionRange)replacement.setSelectionRange(start,end);
    if(content)content.scrollTop=scroll;
  }
  function pollOverview(ctx){
    U.poll=setTimeout(()=>{
      if(ctx.generation!==U.generation)return;
      if(document.querySelector('#detail-dialog[open]'))pollOverview(ctx);
      else refreshPage(ctx).catch(error=>U.status(error.message,true));
    },2500);
  }
  function jobRows(jobs){return jobs.length?U.table(['Job / revision','Time / duration','Status','Step','Action'],jobs.map(job=>{
    const payload=job.payload||{},elapsed=job.started?Math.max(0,Math.round((Date.parse(job.finished||new Date().toISOString())-Date.parse(job.started))/1000)):null;
    const source=payload.commit?.slice(0,12)||((payload.release?.revision)?'Configuration '+payload.release.revision:job.kind==='update_all'&&Array.isArray(payload.targets)?payload.targets.length+' update targets':'');
    const testName=job.kind==='release'&&'test_configuration' in payload?'Tests: '+(payload.test_configuration?.name||'None'):'';
    const label=payload.release?.name||job.subject||({catalog:'Refresh catalogue',download_all:'Download all',update_all:'Update all',check:'Check status'}[job.kind])||job.kind;
    return [E(payload.repository_label||label)+'<small class="muted">'+E([payload.target||payload.version,source,testName].filter(Boolean).join(' · '))+'</small>',E(U.date(job.started||job.created))+(elapsed!==null?'<small class="muted">'+elapsed+' s</small>':''),status(job.status),E(job.error||job.phase),button('Log','job',job.id)];
  })):'';}
  function files(job){return (job.result.files||[]).map(file=>'<p><a class="artifact-link" href="/api/repositories/jobs/'+E(job.id)+'/files/'+E(file.id)+'">'+E(file.name)+'</a> · '+U.bytes(file.bytes)+'<br><small class="muted">SHA-256 '+E(file.sha256)+'</small></p>').join('');}
  U.views.repositories=async ctx=>{
    U.side(ctx,[['overview','Overview'],['connections','Connections'],['tools','Tools'],['releases','Releases'],['test-configurations','Test configurations'],['logs','Logs']]);
    document.querySelector('.submenu-column .column-caption').textContent='Repositories';
    if(ctx.sub==='connections')return U.connectionsView(ctx);
    if(ctx.sub==='tools')return tools(ctx);
    if(ctx.sub==='releases')return releaseView(ctx);
    if(ctx.sub==='test-configurations')return testView(ctx);
    if(ctx.sub==='logs')return logsView(ctx);
    const state=await api('state');if(ctx.generation!==U.generation)return;lastState=state;
    const jobs=state.jobs.filter(active),repos=state.repositories;
    const accounts=new Map();
    for(const item of [...(state.connections||[]).filter(connection=>connection.status==='ready'),...repos]){
      const owner=item.owner||'';if(owner&&!accounts.has(owner.toLowerCase()))accounts.set(owner.toLowerCase(),owner);
    }
    if(account&&!accounts.has(account))account='';
    let html='';
    if(!state.worker.ready)html+='<p class="workspace-notice">The repository and build worker is currently unavailable. Jobs remain queued.</p>';
    const catalogueErrors=(state.connections||[]).filter(connection=>connection.catalog?.error);
    if(catalogueErrors.length)html+=catalogueErrors.map(connection=>'<p class="workspace-notice" role="alert">'+E(connection.name)+' · '+E(connection.catalog.error)+'</p>').join('');
    else if(state.catalog.error)html+='<p class="workspace-notice" role="alert">Last known catalogue · '+E(state.catalog.error)+'</p>';
    html+='<section class="panel" aria-label="Repositories"><div class="repository-filters"><label class="repo-category">Category<select id="repository-category"><option value="">All categories</option>'+(state.categories||[]).map(value=>'<option value="'+E(value)+'"'+(category===value?' selected':'')+'>'+E(value)+'</option>').join('')+'</select></label><label class="repo-account">Account<select id="repository-account"><option value="">All Accounts</option>'+[...accounts].sort((a,b)=>a[1].localeCompare(b[1],'en',{sensitivity:'base'})).map(([key,owner])=>'<option value="'+E(key)+'"'+(account===key?' selected':'')+'>'+E(owner)+'</option>').join('')+'</select></label><label class="repo-search">Search<input id="repository-search" type="search" value="'+E(filter)+'" placeholder="Name, account or description"></label></div>';
    html+=repos.length?'<div class="repository-list"><table class="repository-table" aria-label="Repository overview"><thead><tr><th scope="col">Repository</th><th scope="col">Category</th><th scope="col">Local status</th><th scope="col">Actions</th></tr></thead><tbody>'+repos.map(repo=>{
      const local=repo.local||{},busy=jobs.some(job=>job.subject===repo.name||job.kind.endsWith('_all'));
      const classification=repo.server_metadata?.category||'Undefined';
      const sourceAction=local.status==='outdated'?button('Update','update',repo.name,busy||!repo.available||local.dirty,true):button(local.commit||local.status==='empty'?'Downloaded':'Download','download',repo.name,busy||!!local.commit||local.status==='empty'||!repo.available);
      return '<tr class="repository-entry" data-repo-search="'+E(((repo.display_name||repo.name)+' '+(repo.owner||'')+' '+repo.description).toLowerCase())+'" data-category="'+E(classification)+'" data-account="'+E((repo.owner||'').toLowerCase())+'"><td class="repository-description"><button class="repository-name" data-repo-action="details" data-name="'+E(repo.name)+'">'+E(repo.display_name||repo.name)+'</button>'+(repo.description?'<p class="muted">'+E(repo.description)+'</p>':'')+'<div class="repository-labels"><span class="badge">'+(repo.private?'Private':'Public')+'</span>'+(repo.archived?'<span class="badge">Archived</span>':'')+'</div>'+(!repo.available?'<p class="muted">'+E(repo.unavailable_reason)+'</p>':'')+'</td><td class="repository-category-cell"><span class="badge">'+E(classification)+'</span></td><td class="repository-local" data-local-status="'+E(!repo.available?'unavailable':local.status||'missing')+'">'+status(!repo.available?'Access unavailable':local.status||'missing')+(local.commit?'<p><code>'+E(local.commit.slice(0,12))+'</code> · '+E(local.branch||'')+'</p>':'')+'</td><td class="repository-actions-cell"><span class="repository-owner">'+E(repo.owner||'')+'</span><div class="repo-row-actions">'+sourceAction+button('Build','build_publish',repo.name,!repo.build_release?.enabled||busy,false,repo.build_release?.reason||'Checking build readiness…')+button('Edit','edit',repo.name)+'<label class="repository-publish"><input type="checkbox" data-repository-publish="'+E(repo.name)+'" title="Show in Apps" aria-label="Show in Apps '+E(repo.display_name||repo.name)+'"'+(repo.published||repo.publishing?' checked':'')+((busy&&!repo.publishing)||(!repo.available&&!repo.published&&!repo.publishing)?' disabled':'')+'>Show in Apps'+(repo.publishing?'…':'')+'</label>'+(repo.publish_error?'<p class="form-message" role="alert">'+E(repo.publish_error)+'</p>':'')+'</div></td></tr>';
    }).join('')+'</tbody></table></div><div id="repository-filter-empty" hidden>'+U.empty('No matching repositories','Choose another category or change the search.')+'</div>':U.empty('No catalogue yet','Connect an account and enable its repositories in Connections.');
    html+='</section>';
    if(jobs.length)html+='<section class="panel"><div class="panel-heading"><h2>Active jobs</h2></div>'+jobRows(jobs)+'</section>';
    const repositoryBusy=jobs.some(job=>['catalog','check','download','download_all','update','update_all','connection_delete'].includes(job.kind));
    const canDownload=repos.some(repo=>repo.available&&!repo.local.commit&&!['empty','conflict'].includes(repo.local.status));
    const updates=state.update_count||0;
    const actions=button('Download all','download_all','',repositoryBusy||!canDownload)+button('Update All ('+U.count(updates)+')','update_all','',repositoryBusy||!updates,!!updates)+button('Refresh','catalog','',repositoryBusy);
    U.page(ctx,'Repositories','',html,actions,false);
    U.status(jobs.some(job=>job.kind==='catalog')?'Refreshing repositories…':'Repositories loaded.');
    applyFilter();
    if(ctx.leaf)await details(ctx.leaf);
    if(jobs.length||repos.some(repo=>repo.publishing||repo.build_release?.checking))pollOverview(ctx);
  };
  function applyFilter(){
    const rows=[...document.querySelectorAll('[data-repo-search]')];
    rows.forEach(row=>row.hidden=!row.dataset.repoSearch.includes(filter.toLowerCase())||(!!category&&row.dataset.category!==category)||(!!account&&row.dataset.account!==account));
    const matches=rows.some(row=>!row.hidden),table=document.querySelector('.repository-table');
    if(table)table.hidden=!matches;
    const empty=document.querySelector('#repository-filter-empty');if(empty)empty.hidden=matches;
  }
  async function openBuild(name){
    const generation=U.generation,value=await api('items/'+encodeURIComponent(name)+'/build');
    if(generation!==U.generation)return;
    if(!value.enabled)throw new Error(value.reason);
    if(value.targets.length===1){
      const job=await api('build-publish',{method:'POST',body:{name,target:value.targets[0].id}});
      await showJob(job.id);return;
    }
    const selected=value.targets.find(item=>item.enabled);
    U.open('Build and publish on GitHub','<form id="repository-build-publish" data-name="'+E(name)+'"><p class="muted">Build a pinned source revision and upload the resulting artifacts to GitHub Releases.</p><label>Build target<select name="target">'+value.targets.map(item=>'<option value="'+E(item.id)+'" data-version="'+E(item.version)+'"'+(!item.enabled?' disabled':'')+(item.id===selected.id?' selected':'')+'>'+E(item.name+' · '+item.kind+' · v'+item.version+(item.state==='published'?' · Already published':''))+'</option>').join('')+'</select></label><p class="muted">The release tag is taken from the selected target’s manifest version.</p><div class="form-actions"><button type="submit" class="button primary">Build</button><button type="button" class="button subtle" data-action="close-dialog">Cancel</button></div><p class="form-message" role="alert"></p></form>');
  }
  async function editRepository(name){
    const {repository:repo}=await api('items/'+encodeURIComponent(name));
    U.open('Edit '+(repo.display_name||repo.name),'<form id="repository-metadata" data-name="'+E(repo.name)+'" class="stack-form"><p class="muted">'+E(repo.owner||'')+' / '+E(repo.display_name||repo.name)+'</p><label>Category<select name="category">'+(lastState?.categories||[]).map(value=>'<option value="'+E(value)+'"'+(value===repo.server_metadata.category?' selected':'')+'>'+E(value)+'</option>').join('')+'</select></label><div class="form-actions"><button type="submit" class="button primary">Save</button><button type="button" class="button subtle" data-action="close-dialog">Cancel</button></div><p class="form-message" role="alert"></p></form>');
  }
  async function details(name){
    dialogJob=null;dialogGeneration=U.generation;
    const value=await api('items/'+encodeURIComponent(name)),repo=value.repository,local=repo.local||{};
    let html='<p class="detail-description">'+E(repo.description)+'</p><p><a href="'+E(repo.url)+'" target="_blank" rel="noopener noreferrer">Open on GitHub</a></p>';
    html+='<div class="property-grid"><div>Local status<br>'+status(local.status||'missing')+'</div><div>Default branch<br>'+E(repo.default_branch)+'</div><div>Commit<br><code>'+E(local.commit||'—')+'</code></div><div>Remote commit<br><code>'+E(local.remote_commit||'Not checked yet')+'</code></div></div>';
    if(local.path)html+='<p class="muted">'+E(local.path)+'</p>';
    if(local.branch)html+='<p class="muted">Local branch: '+E(local.branch)+'</p>';
    if(value.tools_error)html+='<p class="workspace-notice">'+E(value.tools_error)+'</p>';
    if(local.detail)html+='<p class="workspace-notice">'+E(local.detail)+'</p>';
    if(!repo.available)html+='<p class="workspace-notice">'+E(repo.unavailable_reason)+'</p>';
    html+='<div class="form-actions">'+button('Download','download',repo.name,!!local.commit||local.status==='empty'||!repo.available)+button('Check status','check',repo.name,!local.commit||!repo.available)+(local.status==='outdated'?button('Update','update',repo.name,!repo.available):'')+'</div>';
    if((local.modules||[]).length){
      const available=repo.available&&!!value.toolset&&!local.dirty&&!value.jobs.some(job=>active(job)&&job.kind==='build');
      html+='<h3>Build targets</h3><form id="repository-build" data-name="'+E(repo.name)+'" data-available="'+available+'"><label>Project target<select name="target">'+local.modules.map(module=>'<option value="'+E(module.id)+'" data-reason="'+E(module.reason||'')+'"'+(module.supported===false?' disabled':'')+'>'+E(module.name+' · '+module.kind+' · '+module.language+(module.version?' · '+module.version:''))+'</option>').join('')+'</select></label><p class="muted">'+(value.toolset?'Active toolset: '+E(value.toolset):'Set up the build tools first.')+' · <a href="#repositories/tools" data-close-repo-dialog>Open Tools</a></p>'+(local.dirty?'<p class="workspace-notice">Local changes detected. A build requires a clean source revision.</p>':'')+(local.profile?.errors?.length?'<p class="workspace-notice">'+local.profile.errors.map(E).join('<br>')+'</p>':'')+'<button type="submit" class="button primary"'+(!available||local.modules.every(module=>module.supported===false)?' disabled':'')+'>Build locally</button><p class="form-message" role="alert"></p></form>';
      if(value.build_tools)html+=U.fold('SDKs and tools used',{compiler:value.build_tools.versions,sources:value.build_tools.sources});
      html+=U.fold('Discovered build profiles',local.modules);
    }else html+='<p class="muted">'+E(local.profile?.reason||'Build targets are discovered after downloading.')+'</p>';
    if(local.profile?.dependencies?.length)html+=U.fold('Build dependencies',local.profile.dependencies);
    if(local.profile?.package_version||local.profile?.cargo_package_version)html+=U.fold('Package versions',{Zig:local.profile.package_version,Cargo:local.profile.cargo_package_version});
    if(local.changes?.length)html+=U.fold('Local changes',local.changes);
    if(value.last_success)html+='<h3>Last successful build</h3>'+button('Artefacts and log','job',value.last_success.id);
    html+='<h3>Recent jobs and builds</h3>'+jobRows(value.jobs);
    const options=await sdk('projects/'+encodeURIComponent(repo.name)),packages=await sdk('catalog');
    html+='<h3>SDK build settings</h3><form id="sdk-project" data-name="'+E(repo.name)+'"><label>SDK<select name="sdk_id"><option value="">Active SDK</option>'+packages.packages.map(item=>'<option value="'+E(item.id)+'"'+(options.sdk_id===item.id?' selected':'')+'>'+E(item.version)+'</option>').join('')+'</select></label><label>Additional library pins (JSON)<textarea name="libraries" rows="5">'+E(JSON.stringify(options.libraries,null,2))+'</textarea></label><p class="muted">Each entry needs id, managed repository and commit (optional path), or path and a complete SDK tree pin. SDK system identities cannot be replaced.</p><label>Technical development snapshot ID (optional)<input name="development_id" value="'+E(options.development_id||'')+'"></label><button class="button" type="submit">Save SDK settings</button><p class="form-message" role="alert"></p></form>';
    U.open(repo.display_name||repo.name,html);
  }
  async function showJob(id,reset=true){
    if(reset){dialogJob=id;dialogOffset=0;dialogText='';dialogGeneration=U.generation;}
    const job=await api('jobs/'+id),log=await api('jobs/'+id+'/log?offset='+dialogOffset);
    if(dialogJob!==id)return;
    dialogOffset=log.offset;dialogText=(dialogText+log.text).slice(-250000);
    const summary=job.result.repositories?'<p>'+(job.kind==='update_all'?U.count(job.result.target_count??job.payload.targets?.length??0)+' update targets · ':'')+U.count(job.result.repositories.filter(item=>item.status!=='failed'&&item.status!=='skipped').length)+' successful · '+U.count(job.result.repositories.filter(item=>item.status==='skipped').length)+' skipped · '+U.count(job.result.failed||0)+' failed</p>'+U.fold('Individual results',job.result.repositories):'';
    const testInfo=job.kind==='release'&&'test_configuration' in (job.payload||{})?'<p class="muted">Test configuration: '+E(job.payload.test_configuration?job.payload.test_configuration.name+' · Revision '+job.payload.test_configuration.revision:'None')+'</p>':'';
    const html='<div class="detail-status">'+status(job.status)+'<span>'+E(job.phase)+'</span>'+(active(job)?button('Cancel','cancel',id):'')+'</div>'+testInfo+(job.result.github_release?.published?'<p><a class="artifact-link" href="'+E(job.result.github_release.release_url)+'" target="_blank" rel="noopener noreferrer">Open GitHub release '+E(job.result.github_release.tag)+'</a></p>':job.result.local_build?'<p class="workspace-notice">Local artifacts are available. GitHub publication is not complete; start Build again to retry the upload.</p>':'')+(job.error?'<p class="workspace-notice">'+E(job.error)+'</p>':'')+summary+files(job)+'<pre class="build-log" tabindex="0">'+E(dialogText||'Job waiting for the worker …')+'</pre>';
    const dialog=document.querySelector('#detail-dialog'),oldLog=dialog?.querySelector('.build-log');
    const oldScroll=oldLog?.scrollTop||0,atBottom=!oldLog||oldLog.scrollHeight-oldLog.clientHeight-oldScroll<20;
    const focused=document.activeElement,focusLog=focused===oldLog,focusCancel=focused?.dataset.repoAction==='cancel';
    if(reset||!dialog?.open)U.open('Job · '+(job.payload?.release?.name||job.subject||job.kind),html);
    else dialog.querySelector('.dialog-body').innerHTML=html;
    const logNode=document.querySelector('.build-log');if(logNode)logNode.scrollTop=atBottom?logNode.scrollHeight:oldScroll;
    if(focusLog)logNode?.focus({preventScroll:true});
    if(focusCancel)document.querySelector('[data-repo-action="cancel"]')?.focus({preventScroll:true});
    if(active(job))setTimeout(()=>{if(dialogJob===id&&document.querySelector('#detail-dialog[open]'))showJob(id,false).catch(error=>U.status(error.message,true));},1500);
  }
  async function tools(ctx){
    const value=await sdk('tools');if(ctx.generation!==U.generation)return;
    const ready=value.packages.filter(item=>item.status==='ready');
    let html='<section class="panel panel-body"><h2>SDK packages</h2><p>The SDK selects build profiles, exact tools, Contract and system libraries. Package originals are immutable.</p><form id="sdk-import"><label>Official SDK release URL<input name="release_url" type="url" required placeholder="https://github.com/R4OSDev/r4os-sdk/releases/tag/v0.64.145"></label><button class="button primary" type="submit">Import and verify SDK</button><p class="form-message" role="alert"></p></form></section>';
    html+='<section class="panel panel-body"><h2>Build environment</h2><p>Host: '+E(value.host)+' · Active SDK: '+E(value.sdk?.version||'Not configured')+'</p><form id="sdk-setup"><label>Verified SDK<select name="sdk_id" required>'+ready.map(item=>'<option value="'+E(item.id)+'"'+(item.id===value.sdk?.id?' selected':'')+'>'+E(item.version+' · '+item.source.commit.slice(0,12))+'</option>').join('')+'</select></label><fieldset><legend>SDK-owned setup</legend>'+['zig','rust','limine','qemu'].map(name=>'<label><input type="checkbox" name="tools" value="'+name+'"'+(['zig','rust'].includes(name)?' checked':'')+'> '+E(name)+'</label>').join('')+'</fieldset><p class="muted">Graphics, shader and platform prerequisites are verified by their native owner builders.</p><button type="submit" class="button primary"'+(!ready.length?' disabled':'')+'>Set up and activate</button> '+button('Check active environment','tools_check','',!value.active)+'<p class="form-message" role="alert"></p></form>'+U.table(['Tool','SDK requirement','Installed','State'],value.components.map(item=>[E(item.name),E(item.required),E(item.installed||'—'),E(item.state)]))+'</section>';
    if(value.errors?.length)html+='<p class="workspace-notice">'+E(value.errors.join(' · '))+'</p>';
    for(const item of value.packages){
      html+='<section class="panel panel-body"><h2>SDK '+E(item.version)+'</h2><p>'+E(item.status)+(item.error?' · '+E(item.error):'')+'</p><p><code>'+E(item.id)+'</code><br>Source: '+E(item.source.commit)+'</p>';
      if(item.status==='ready'){
        html+=button('Verify package','sdk_verify',item.id)+U.table(['Profile','Language / role','Tools / components','Action'],Object.entries(item.profiles).map(([id,profile])=>[E(id),E(profile.language+' / '+profile.role),E(profile.toolset+' / '+profile.components),button('Describe','sdk_describe',item.id+'|'+id)+button('Resolve','sdk_resolve',item.id+'|'+id)]));
        html+=U.fold('Original profile hashes',item.profile_sources)+U.table(['Original asset','Role','Bytes','Download'],item.assets.map(asset=>[E(asset.path),E(asset.role),U.bytes(asset.bytes),'<a class="artifact-link" href="/api/sdk/assets/'+E(item.id)+'/'+E(asset.path)+'">Download</a>']));
      }
      html+='</section>';
    }
    html+='<section class="panel panel-body"><h2>Installed SDK environments</h2>'+U.table(['Environment','Status','Action'],value.toolsets.map(item=>[E(item.id),E(item.id===value.active?'Active':item.status),button('Activate','tools_activate',item.id,item.id===value.active||item.status!=='ready')]))+'</section>';
    html+='<section class="panel panel-body"><h2>Technical development snapshot</h2><p>An explicit snapshot pins managed SDK, Contract and Libraries sources. Changed sources make the snapshot stale. Technical builds cannot be published as qualified releases.</p><form id="sdk-development"><label>SDK<select name="sdk_id">'+ready.map(item=>'<option value="'+E(item.id)+'">'+E(item.version)+'</option>').join('')+'</select></label><label>Profile<select name="profile">'+[...new Set(ready.flatMap(item=>Object.keys(item.profiles)))].map(id=>'<option>'+E(id)+'</option>').join('')+'</select></label>'+['sdk','contract','libraries'].map(role=>'<label>'+E(role)+' repository ID<input name="'+role+'_repository" required placeholder="r4os-'+role+'"></label>').join('')+'<button class="button" type="submit">Create technical snapshot</button><p class="form-message" role="alert"></p></form></section>';
    html+='<section class="panel panel-body"><h2>Obsolete installations</h2><p>After SDK verification, remove obsolete local build environments. Historical receipts and application data are retained.</p>'+button('Remove obsolete installations','sdk_cleanup','',!value.active)+'</section>'+jobRows(value.jobs);
    U.page(ctx,'Tools','',html);
    if(value.jobs.some(active))U.poll=setTimeout(()=>{if(ctx.generation===U.generation&&!document.querySelector('#detail-dialog[open]'))refreshPage(ctx);},2500);
  }

  function releaseRepository(repo,member){
    const modules=(repo.local.modules||[]).filter(module=>module.kind!=='HostTools'),wanted=member.targets||[];
    const chosen=modules.filter(module=>member.selected&&(!wanted.length||wanted.includes(module.id))).map(module=>module.id).sort();
    const origin=member.origin==='manual'?'Manual':member.origin==='manifest'?'From repository preset':member.origin==='base'?'System/build foundation':'Not assigned yet';
    let html='<div class="release-repository" data-release-search="'+E(((repo.display_name||repo.name)+' '+(repo.owner||'')).toLowerCase())+'"><label class="release-repo-option"><input form="release-edit" type="checkbox" name="selected" value="'+E(repo.name)+'"'+(member.selected?' checked':'')+'><span>'+E(repo.display_name||repo.name)+'<small class="muted">'+E(origin)+(modules.length?'':' · No image component; source/build foundation')+'</small></span></label>';
    if(modules.length)html+='<details class="release-targets" data-target-owner="'+E(repo.name)+'" data-initial-targets="'+E(JSON.stringify(chosen))+'"><summary>'+modules.length+' build target'+(modules.length===1?'':'s')+' available</summary>'+modules.map(module=>'<label><input form="release-edit" type="checkbox" name="target:'+E(repo.name)+'" value="'+E(module.id)+'"'+(chosen.includes(module.id)?' checked':'')+'> '+E(module.name+' · '+module.kind)+(module.target?'<small class="muted">'+E(module.target)+'</small>':'')+'</label>').join('')+'</details>';
    return html+'</div>';
  }
  async function releaseView(ctx){
    const value=await api('releases');if(ctx.generation!==U.generation)return;
    U.context(ctx,[['new','+ New release type'],...value.releases.map(item=>[item.id,item.name])]);
    const selected=ctx.leaf||value.releases[0]?.id||'new';
    if(selected==='new'){
      U.page(ctx,'New release type','','<form id="release-new" class="panel panel-body"><div class="form-grid"><label>Name<input name="name" required maxlength="80"></label><label>Template<select name="template"><option value="">None</option>'+value.releases.map(item=>'<option value="'+E(item.id)+'">'+E(item.name)+'</option>').join('')+'</select></label></div><p class="muted">Test configuration: None</p><button class="button primary" type="submit">Create release type</button><p class="form-message" role="alert"></p></form>');return;
    }
    if(!selected)return;
    const detail=await api('releases/'+selected);if(ctx.generation!==U.generation)return;
    const release=detail.release;
    const logsLink='#repositories/logs/releases?release='+encodeURIComponent(release.id);
    let html='<div class="release-top-grid"><section class="panel panel-body"><h2>Build release</h2><form id="release-build" data-id="'+E(release.id)+'" data-revision="'+release.revision+'"><label>R4OS version<input name="version" required pattern="[0-9]+\\.[0-9]+\\.[0-9]+" value="'+E(detail.default_version||'')+'" placeholder="e.g. 0.87.52"></label><p class="muted">'+U.count(detail.components.length)+' components · Configuration revision '+release.revision+'</p>'+(detail.issues.length?'<details class="release-issues"><summary>'+U.count(detail.issues.length)+' requirements unmet</summary><p class="workspace-notice">'+detail.issues.map(E).join('<br>')+'</p></details>':'')+'<button class="button primary" type="submit"'+(!detail.ready?' disabled':'')+'>Build release</button><p class="form-message" role="alert"></p></form><p><a class="artifact-link" href="'+E(logsLink)+'">Open release history and logs</a></p></section>';
    html+='<form id="release-edit" data-id="'+E(release.id)+'" data-revision="'+release.revision+'" data-saved-name="'+E(release.name)+'" class="panel panel-body stack-form"><h2>Release type</h2><label>Name<input name="name" value="'+E(release.name)+'" required maxlength="80"></label><label>Test configuration<select name="test_configuration"><option value="">None</option>'+(value.test_configurations||[]).map(item=>'<option value="'+E(item.id)+'"'+(item.id===release.test_configuration?' selected':'')+'>'+E(item.name)+'</option>').join('')+'</select></label><label>Description<textarea name="description" maxlength="4000" rows="3">'+E(release.description)+'</textarea></label><div class="form-actions"><button class="button primary" type="submit">Save</button>'+button('Delete release type','release_delete',release.id)+'</div><span id="release-dirty" class="muted"></span><p class="form-message" role="alert"></p></form></div>';
    html+='<section class="panel panel-body"><h2>Repository assignment</h2><label>Search<input id="release-repo-search" type="search" placeholder="Filter repositories"></label><div class="release-repo-list">'+value.repositories.map(repo=>{
      const member=release.members[repo.name]||{};
      return releaseRepository(repo,member);
    }).join('')+'</div><div class="form-actions"><button class="button primary" form="release-edit" type="submit">Save assignments and details</button></div>'+U.fold('Saved components',detail.components.map(module=>({repository:module.repository,name:module.name,kind:module.kind,target:module.target,version:module.version})))+'</section>';
    U.page(ctx,release.name,'',html);
  }
  async function logsView(ctx){
    const [requested,query='']=(ctx.leaf||'all').split('?'),params=new URLSearchParams(query);
    const categories=[['all','All'],['repositories','Repositories'],['tools','Tools'],['builds','Builds'],['releases','Releases']];
    const category=categories.some(([key])=>key===requested)?requested:'all';
    U.context({...ctx,leaf:category},categories);
    const page=Math.max(0,Number.parseInt(params.get('page')||'0',10)||0),release=params.get('release');
    const search=new URLSearchParams({category,limit:'50',offset:String(page*50)});
    if(category==='releases'&&release)search.set('subject','release:'+release);
    const value=await api('jobs?'+search);if(ctx.generation!==U.generation)return;
    const currentName=value.jobs[0]?.payload?.release?.name||value.last_success?.payload?.release?.name||release;
    let html=category==='releases'&&release?'<p class="muted">Release type: '+E(currentName)+' · <a class="artifact-link" href="#repositories/logs/releases">All releases</a></p>':'';
    if(value.last_success)html+='<p>'+button('Last successful release','job',value.last_success.id)+'</p>';
    html+='<section class="panel">'+(value.jobs.length?jobRows(value.jobs):U.empty('No jobs yet','No jobs are available in this selection yet.'));
    const link=target=>{const next=new URLSearchParams();if(release&&category==='releases')next.set('release',release);next.set('page',String(target));return '#repositories/logs/'+category+'?'+next;};
    html+='<div class="pagination"><span>'+U.count(value.total)+' jobs · Page '+(page+1)+'</span><div>'+(page>0?'<a class="button subtle" href="'+E(link(page-1))+'">Previous</a>':'')+(value.offset+value.jobs.length<value.total?'<a class="button subtle" href="'+E(link(page+1))+'">Next</a>':'')+'</div></div></section>';
    U.page(ctx,'Logs · '+categories.find(([key])=>key===category)[1],'',html);
    U.poll=setTimeout(()=>{if(ctx.generation===U.generation&&!document.querySelector('#detail-dialog[open]'))refreshPage(ctx);},5000);
  }

  let testRepositories=[];
  function testFileRow(entry={}){
    return '<div class="test-file-row" data-repository="'+E(entry.repository||'')+'" data-source="'+E(entry.source||'')+'" data-commit="'+E(entry.commit||'')+'"><label>Repository<select data-field="repository" required><option value="">Select</option>'+testRepositories.map(repo=>'<option value="'+E(repo.name)+'"'+(repo.name===entry.repository?' selected':'')+'>'+E((repo.owner?repo.owner+'/':'')+(repo.display_name||repo.name))+'</option>').join('')+'</select></label><label>Source file or directory<input data-field="source" required value="'+E(entry.source||'')+'" placeholder="e.g. TestInjection"></label><label>Image target<input data-field="target" required value="'+E(entry.target||'/')+'" placeholder="/">'+(entry.commit?'<small class="muted">Source revision '+E(entry.commit.slice(0,12))+'</small>':'')+'</label>'+button('Remove','test_file_remove')+'</div>';
  }
  async function testView(ctx){
    const value=await api('test-configurations');if(ctx.generation!==U.generation)return;
    testRepositories=value.repositories;
    U.context(ctx,[['new','+ New test configuration'],...value.test_configurations.map(item=>[item.id,item.name])]);
    const selected=ctx.leaf||value.test_configurations[0]?.id||'new';
    const config=selected==='new'?{name:'',description:'',definition:{extends:null,targets:[],files:[],settings:null,startup:null},origin:{}}:value.test_configurations.find(item=>item.id===selected);
    if(!config)throw new Error('Test configuration not found.');
    const definition=config.definition;
    let html='<form id="test-configuration" class="stack-form" data-id="'+E(config.id||'')+'" data-revision="'+E(config.revision||'')+'" data-saved-name="'+E(config.name)+'">';
    if(config.origin.pending)html+='<p class="workspace-notice">Existing test presets are imported after downloading and checking Distribution and Project.</p>';
    html+='<section class="panel panel-body stack-form"><div class="form-grid"><label>Name<input name="name" required maxlength="80" value="'+E(config.name)+'"></label><label>Extends<select name="extends"><option value="">None</option>'+value.test_configurations.filter(item=>item.id!==config.id).map(item=>'<option value="'+E(item.id)+'"'+(definition.extends===item.id?' selected':'')+'>'+E(item.name)+'</option>').join('')+'</select></label></div><label>Description<textarea name="description" rows="2" maxlength="4000">'+E(config.description)+'</textarea></label></section>';
    html+='<section class="panel panel-body stack-form"><h2>Test settings</h2><label>Required module targets · one target path per line<textarea name="targets" rows="5" placeholder="/R4OS/SOFTWARE/TERMINAL/DIAG/EXAMPLE.R4X">'+E(definition.targets.join('\n'))+'</textarea></label><label class="toggle-control"><input type="checkbox" name="use_settings" data-test-toggle="settings"'+(definition.settings!==null?' checked':'')+'> Use custom test settings</label><label>Test settings<textarea name="settings" rows="6"'+(definition.settings===null?' disabled':'')+'>'+E(definition.settings||'')+'</textarea></label><label class="toggle-control"><input type="checkbox" name="use_startup" data-test-toggle="startup"'+(definition.startup!==null?' checked':'')+'> Use custom startup commands</label><label>Startup commands<textarea name="startup" rows="8"'+(definition.startup===null?' disabled':'')+'>'+E(definition.startup||'')+'</textarea></label></section>';
    html+='<section class="panel panel-body"><h2>Test files</h2><div id="test-files">'+definition.files.map(testFileRow).join('')+'</div>'+button('Add file source','test_file_add')+'</section><div class="form-actions"><button class="button primary" type="submit">'+(config.id?'Save':'Create test configuration')+'</button>'+(config.id?button('Delete test configuration','test_delete',config.id):'')+'</div><p class="form-message" role="alert"></p></form>';
    U.page(ctx,config.id?config.name:'New test configuration','',html);
  }
  document.addEventListener('close',event=>{
    if(event.target.id==='detail-dialog'){
      const job=dialogJob;
      dialogJob=null;
      if(dialogGeneration!==U.generation)return;
      if(job&&location.hash.startsWith('#repositories')&&!document.querySelector('#release-dirty')?.textContent)refreshPage(U.current);
      else if(dialogTrigger?.isConnected)dialogTrigger.focus({preventScroll:true});
    }
  },true);
  document.addEventListener('change',event=>{
    if(event.target.dataset.testToggle){
      event.target.form.elements.namedItem(event.target.dataset.testToggle).disabled=!event.target.checked;
    }
    const form=event.target.form;if(form?.id!=='release-edit')return;
    const row=event.target.closest('.release-repository');if(!row)return;
    if(event.target.name==='selected')row.querySelectorAll('.release-targets input').forEach(input=>input.checked=event.target.checked);
    else if(event.target.name.startsWith('target:'))row.querySelector('[name="selected"]').checked=!!row.querySelector('.release-targets input:checked');
  });
  document.addEventListener('input',event=>{
    if(event.target.id==='repository-search'){filter=event.target.value;applyFilter();}
    if(event.target.id==='release-repo-search')document.querySelectorAll('[data-release-search]').forEach(row=>row.hidden=!row.dataset.releaseSearch.includes(event.target.value.toLowerCase()));
    if(event.target.form?.id==='release-edit'){
      const dirty=document.querySelector('#release-dirty');if(dirty)dirty.textContent='Unsaved changes';
      const build=document.querySelector('#release-build button[type="submit"]');if(build)build.disabled=true;
    }
  });
  document.addEventListener('change',async event=>{
    const checkbox=event.target;if(!checkbox.matches('[data-repository-publish]'))return;
    const selected=checkbox.checked;checkbox.disabled=true;
    try{
      const repo=await api('items/'+encodeURIComponent(checkbox.dataset.repositoryPublish)+'/publication',{method:'PUT',body:{published:selected}});
      if(checkbox.isConnected)checkbox.checked=repo.published||repo.publishing;
      const cached=lastState?.repositories.find(item=>item.name===repo.name);if(cached)Object.assign(cached,{published:repo.published,publishing:repo.publishing});
      if(repo.publishing)pollOverview(U.current);
      U.status(repo.publishing?'Adding app to the list…':selected?'App listed. Open Apps → Overview to edit its settings.':'App removed from the Apps overview.');
    }catch(error){if(checkbox.isConnected)checkbox.checked=!selected;U.status(error.message,true);}
    finally{if(checkbox.isConnected)checkbox.disabled=false;}
  });
  document.addEventListener('change',event=>{
    if(event.target.id==='repository-category'){category=event.target.value;applyFilter();}
    if(event.target.id==='repository-account'){account=event.target.value;applyFilter();}
  });
  document.addEventListener('click',async event=>{
    if(event.target.closest('[data-close-repo-dialog]'))document.querySelector('#detail-dialog')?.close();
    const node=event.target.closest('[data-repo-action]');if(!node)return;
    const action=node.dataset.repoAction,name=node.dataset.name;
    if(action==='test_file_add'){document.querySelector('#test-files').insertAdjacentHTML('beforeend',testFileRow());return;}
    if(action==='test_file_remove'){node.closest('.test-file-row').remove();return;}
    if(['details','job','edit'].includes(action))dialogTrigger=node;
    node.disabled=true;
    try{
      if(action==='release_delete'||action==='test_delete'){
        const isRelease=action==='release_delete',form=document.querySelector(isRelease?'#release-edit':'#test-configuration');
        const label=isRelease?'Release type':'Test configuration';
        if(!window.confirm('Delete '+label+' “'+form.dataset.savedName+'”?'))return;
        await api((isRelease?'releases/':'test-configurations/')+name,{method:'DELETE',body:{revision:Number(form.dataset.revision)}});
        const hash='#repositories/'+(isRelease?'releases':'test-configurations');
        if(location.hash===hash)await U.navigate();else location.hash=hash;
        U.status(label+' deleted.');return;
      }
      if(action==='build_publish'){await openBuild(name);return;}
      if(action==='details'){await details(name);return;}
      if(action==='edit'){await editRepository(name);return;}
      if(action==='job'){await showJob(name);return;}
      if(action==='cancel'){await api('jobs/'+name+'/cancel',{method:'POST',body:{}});await showJob(name);return;}
      if(action==='catalog'){
        const ctx=U.current;
        await api('actions/catalog',{method:'POST',body:{}});
        if(ctx.generation===U.generation)await refreshPage(ctx);
        return;
      }
      let job;
      if(action==='sdk_verify'){job=await sdk('packages/'+name+'/verify',{method:'POST',body:{}});}
      else if(action==='sdk_describe'||action==='sdk_resolve'){const [id,profile]=name.split('|');job=await sdk('profiles/'+id+'/'+action.slice(4),{method:'POST',body:{profile}});}
      else if(action==='sdk_cleanup'){if(!window.confirm('Remove inventoried obsolete local SDK installations after verification?'))return;job=await sdk('cleanup',{method:'POST',body:{confirmed:true}});}
      else if(action.startsWith('tools_'))job=await api('tools/actions/'+action.slice(6).replace('_component',''),{method:'POST',body:action.endsWith('_component')?{component:name}:{id:name||undefined}});
      else job=await api('actions/'+action,{method:'POST',body:{name:name||undefined}});
      await showJob(job.id);U.status('Job started.');
    }catch(error){U.status(error.message,true);}finally{node.disabled=false;}
  });
  document.addEventListener('submit',async event=>{
    if(['sdk-import','sdk-setup','sdk-project','sdk-development'].includes(event.target.id)){
      event.preventDefault();const form=event.target,submit=form.querySelector('[type=submit]'),values=new FormData(form);submit.disabled=true;
      try{
        let job;
        if(form.id==='sdk-import')job=await sdk('imports',{method:'POST',body:{release_url:values.get('release_url')}});
        else if(form.id==='sdk-setup')job=await api('tools/actions/install',{method:'POST',body:{sdk_id:values.get('sdk_id'),tools:values.getAll('tools')}});
        else if(form.id==='sdk-development')job=await sdk('development',{method:'POST',body:Object.fromEntries(values)});
        else{await sdk('projects/'+encodeURIComponent(form.dataset.name),{method:'PUT',body:{sdk_id:values.get('sdk_id')||null,libraries:JSON.parse(values.get('libraries')),development_id:values.get('development_id')||null}});U.status('SDK settings saved.');return;}
        await showJob(job.id);
      }catch(error){form.querySelector('.form-message').textContent=error.message;}finally{submit.disabled=false;}return;
    }

    if(event.target.id==='repository-metadata'){
      event.preventDefault();const form=event.target,submit=form.querySelector('[type=submit]');submit.disabled=true;
      try{
        await api('items/'+encodeURIComponent(form.dataset.name)+'/metadata',{method:'PUT',body:{category:new FormData(form).get('category')}});
        await refreshPage(U.current);U.status('Repository category saved.');
      }catch(error){form.querySelector('.form-message').textContent=error.message;submit.disabled=false;}
      return;
    }
    const form=event.target;if(!['repository-build','repository-build-publish','release-new','release-edit','release-build','test-configuration'].includes(form.id))return;
    event.preventDefault();const submit=form.querySelector('[type="submit"]');submit.disabled=true;
    try{
      const values=new FormData(form);let result;
      if(form.id==='repository-build-publish')result=await api('build-publish',{method:'POST',body:{name:form.dataset.name,target:values.get('target')}});
      else if(form.id==='repository-build')result=await api('build',{method:'POST',body:{name:form.dataset.name,target:values.get('target')}});
      else if(form.id==='release-new'){
        const template=values.get('template');
        result=await api('releases',{method:'POST',body:{name:values.get('name'),template:template||undefined,test_configuration:null}});
        location.hash='repositories/releases/'+result.id;return;
      }else if(form.id==='release-edit'){
        const targets={};document.querySelectorAll('[data-target-owner]').forEach(group=>{
          const chosen=[...group.querySelectorAll('input:checked')].map(input=>input.value).sort();
          if(JSON.stringify(chosen)!==group.dataset.initialTargets)targets[group.dataset.targetOwner]=chosen;
        });
        await api('releases/'+form.dataset.id,{method:'PUT',body:{name:values.get('name'),description:values.get('description'),test_configuration:values.get('test_configuration')||null,selected:values.getAll('selected'),targets,revision:Number(form.dataset.revision)}});
        await U.navigate();U.status('Release configuration saved.');return;
      }else if(form.id==='test-configuration'){
        const fileInputs=[...form.querySelectorAll('.test-file-row')].map(row=>{
          const entry=Object.fromEntries([...row.querySelectorAll('[data-field]')].map(input=>[input.dataset.field,input.value]));
          if(entry.repository===row.dataset.repository&&entry.source===row.dataset.source&&row.dataset.commit)entry.commit=row.dataset.commit;
          return entry;
        });
        const definition={extends:values.get('extends')||null,targets:String(values.get('targets')||'').split(/\r?\n/).map(line=>line.trim()).filter(Boolean),settings:values.has('use_settings')?values.get('settings'):null,startup:values.has('use_startup')?values.get('startup'):null,files:fileInputs};
        const id=form.dataset.id;
        result=await api('test-configurations'+(id?'/'+id:''),{method:id?'PUT':'POST',body:{name:values.get('name'),description:values.get('description'),definition,revision:id?Number(form.dataset.revision):undefined}});
        if(id)await U.navigate();else location.hash='repositories/test-configurations/'+result.id;
        U.status('Test configuration saved.');return;
      }else result=await api('releases/'+form.dataset.id+'/build',{method:'POST',body:{version:values.get('version'),revision:Number(form.dataset.revision)}});
      await showJob(result.id);
    }catch(error){form.querySelector('.form-message').textContent=error.message;U.status(error.message,true);}finally{submit.disabled=false;}
  });
})();
