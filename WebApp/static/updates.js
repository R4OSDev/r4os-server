"use strict";
(() => {
  const U=window.R4,E=U.esc;
  let snapshot=null;
  let uploadOffset=0,lastFilter='';
  const kind = value => value==='manifest'?'Release manifest':'Package';
  const title = item => item.metadata?.title || item.filename || kind(item.kind)+' · '+item.id.slice(0,8);
  const row = item => U.record('<h2 class="record-name">'+E(title(item))+'</h2><span class="record-subline">Release '+E(item.metadata?.release || '—')+'</span><p class="record-description">'+E(item.filename || item.id)+'</p>'+U.fields([['Type',E(kind(item.kind))],['Validation',U.badge(item.status)],['Size',E(U.bytes(item.size))],['Received',E(U.date(item.created_at))]]),'<button type="button" class="button subtle" data-update-id="'+E(item.id)+'">Details</button>');
  const incoming = items => items.length?U.records(items.map(row),'Incoming uploads'):U.empty('No uploads yet','Uploaded packages and release manifests appear here with their validation status.');
  const panel = (title,body) => '<section class="panel"><div class="panel-heading"><h2>'+E(title)+'</h2></div>'+body+'</section>';
  const recordsPanel = (title,body) => '<section class="record-section"><div class="panel-heading"><h2>'+E(title)+'</h2></div>'+body+'</section>';
  const entries = [['overview','Overview'],['incoming','Incoming'],['released','Published'],['catalog','Catalogue'],['audit','Log']];
  const selected = (ctx,items) => items.filter(item=>ctx.leaf==='packages'?item.kind==='package':ctx.leaf==='manifests'?item.kind==='manifest':true);
  const released = state => [...state.released.map(item=>({...item,kind:'package'})),...state.released_manifests.map(item=>({...item,kind:'manifest'}))];
  const eventLabels={upload_received:'Upload received',validation_valid:'Validation passed',validation_invalid:'Validation failed',published:'Published',publication_rejected:'Publication rejected',upload_failed:'Transfer cancelled'};

  U.views.updates=async ctx => {
    U.side(ctx,entries);
    if(['incoming','released'].includes(ctx.sub))U.context(ctx,[['all','All files'],['packages','Packages'],['manifests','Release manifests']]);
    const state=await U.api('updates/state');if(ctx.generation!==U.generation)return;snapshot=state;
    const items=state.uploads.items,latest=state.catalog.releases[0];
    let body='',name='Updates',description='Validate packages, serve releases and review publications.';
    if(ctx.sub==='overview'){
      const waiting=Object.entries(state.upload_counts).filter(([name])=>name!=='published').reduce((sum,[,count])=>sum+count,0);
      body='<div class="metric-grid">'+U.card('Incoming',U.count(waiting),'Not published yet')+U.card('Packages',U.count(state.released.length),'Published, immutable')+U.card('Current release',latest?.release || '—',latest?'Complete in catalogue':'No target release yet')+'</div>';
      body+=recordsPanel('Recent uploads',incoming(items.slice(0,6)));
    } else if(ctx.sub==='incoming'){
      name='Incoming';description='Transfer and content validation are separate steps.';
      const filter=ctx.leaf||'all';if(filter!==lastFilter){uploadOffset=0;lastFilter=filter;}
      const query=new URLSearchParams({limit:'50',offset:String(uploadOffset),exclude_published:'true'});
      if(filter==='packages' || filter==='manifests')query.set('kind',filter==='packages'?'package':'manifest');
      const page=await U.api('updates/uploads?'+query);if(ctx.generation!==U.generation)return;
      body=panel('New upload',uploadForm())+recordsPanel('Packages and manifests',incoming(page.items)+'<div class="pagination"><span>'+E(U.count(page.total))+' uploads</span><div><button class="button subtle" data-update-page="previous"'+(uploadOffset===0?' disabled':'')+'>Previous</button><button class="button subtle" data-update-page="next"'+(uploadOffset+page.items.length>=page.total?' disabled':'')+'>Next</button></div></div>');
      if(page.items.some(item=>['queued','validating','receiving'].includes(item.status)))schedule(ctx);
    } else if(ctx.sub==='released'){
      name='Published';description='Published files remain unchanged.';
      const files=selected(ctx,released(state));
      body=recordsPanel('Published files',files.length?U.records(files.map(item=>U.record('<h2 class="record-name">'+E(item.metadata?.title || item.filename)+'</h2><span class="record-subline">Release '+E(item.metadata?.release || '—')+'</span><p class="record-description">'+E(item.filename)+'</p>'+U.fields([['Type',E(kind(item.kind))],['Size',E(U.bytes(item.size))],['Publication',U.badge(item.valid?'published':'invalid')]]),'<button type="button" class="button subtle" data-released-file="'+E(item.filename)+'">Details</button>')),'Published files'):U.empty('No publications yet','Valid uploads can be published after validation.'));
    } else if(ctx.sub==='catalog'){
      name='Catalogue';description='Clients receive the highest fully published release.';
      if(!latest)body=panel('Target release',U.empty('No published release yet','Publish the packages first, then their release manifest.'));
      else {
        body='<div class="metric-grid">'+U.card('Release',latest.release)+U.card('Packages',U.count(latest.packages.length))+U.card('Catalogue',state.catalog.schema,'Schema')+'</div>';
        body+=recordsPanel('Installation order',U.records(latest.packages.map(item=>U.record('<h2 class="record-name">'+E(item.title)+'</h2><span class="record-subline">Version '+E(item.package_version)+'</span><p class="record-description">'+E(item.filename)+'</p>'+U.fields([['Install order',E(item.install_order+1)],['Activation',E(item.activation)],['Size',E(U.bytes(item.size))],['Package ID',E(item.id)]]),'<button type="button" class="button subtle" data-released-file="'+E(item.filename)+'">Details</button>')),'Installation order'));
        body+=U.fold('Profiles and target components',latest.manifest.profiles);
      }
      if(state.catalog_errors.length)body+=panel('Releases unavailable for delivery',U.raw(state.catalog_errors));
    } else if(ctx.sub==='audit'){
      name='Log';description='Update service transfers, validations and publications.';
      body=recordsPanel('Recent events',state.audit.length?U.records(state.audit.map(item=>U.record('<h2 class="record-name">'+E(eventLabels[item.event] || item.event)+'</h2><span class="record-subline">'+E(U.date(item.created_at))+'</span><p class="record-description">'+E(item.note)+'</p>'+U.fields([['Source',E(item.ip || '—')]]))),'Update events'):U.empty('No events yet','The first transfers and publications will appear here.'));
    }
    U.page(ctx,name,description,body,ctx.sub!=='incoming'?'<a class="button primary" href="#updates/incoming">Upload</a>':'');
    U.status('Update inventory loaded.');
  };

  function schedule(ctx){U.poll=setTimeout(async()=>{if(ctx.generation!==U.generation)return;if(U.busy || document.querySelector('#detail-dialog')?.open)schedule(ctx);else {const scroll=U.scrollContainer().scrollTop;await U.navigate();if(U.current.view===ctx.view&&U.current.sub===ctx.sub&&U.current.leaf===ctx.leaf)U.scrollContainer().scrollTop=scroll;}},1500);}
  function uploadForm(){return '<form id="update-upload" class="panel-body"><p class="muted form-intro">Packages and original release manifests are validated after transfer. Publication is a separate step.</p><div class="form-grid"><label>File type<select name="kind" id="upload-kind"><option value="package">Update package (.R4U)</option><option value="manifest">Release manifest (.JSON)</option></select></label><label>File<input id="update-file" name="file" type="file" accept=".R4U,.r4u" required></label></div><div class="form-actions"><button class="button primary" type="submit">Upload</button><button class="button danger" id="cancel-upload" type="button" hidden>Cancel</button><span id="upload-caption" class="muted"></span></div><progress id="upload-progress" max="100" value="0" hidden></progress><p id="upload-error" class="form-message" role="alert"></p></form>';}

  const details = item => {
    const m=item.metadata;
    let html='<div class="detail-status">'+U.badge(item.status || (item.valid?'published':'invalid'))+'<code>'+E(item.filename || item.id || '')+'</code></div>';
    if(item.error)html+='<div class="notice error" role="alert">'+E(item.error)+'</div>';
    if(m){
      html+='<div class="metric-grid">'+U.card('Release',m.release)+U.card('Size',U.bytes(item.size))+U.card('Package version',m.package_version || '—')+'</div>';
      if(m.description)html+='<p class="detail-description">'+E(m.description)+'</p>';
      html+='<dl class="property-grid"><div><dt>File</dt><dd>'+E(item.filename)+'</dd></div><div><dt>SHA-256</dt><dd class="hash">'+E(m.sha256)+'</dd></div></dl>';
      if(m.components)html+=panel('Components',U.table(['Name','Type','Version','Target'],m.components.map(c=>[E(c.name),E(c.kind),E(c.version),E(c.target)])));
      if(m.requirements)html+=U.fold('Requirements',m.requirements);
      if(m.profiles)html+=U.fold('Release profiles and target state',m.profiles);
      html+=U.fold('Complete validation metadata',m);
    } else if(!item.error)html+='<p class="muted">The file is still being validated.</p>';
    if(item.publication && item.status!=='published'){
      const ready=item.publication.status==='ready';
      html+='<section class="publication-review"><h3>Publication</h3><p class="muted">'+E(ready?(item.kind==='manifest'?'All associated packages are published. This manifest completes the release. No more packages can be added afterwards.':'Package validation is complete. Review components, versions and requirements before publishing.'):item.publication.reason || 'Validation is still in progress.')+'</p>'+(ready?'<button class="button primary" data-publish-update="'+E(item.id)+'">'+(item.kind==='manifest'?'Complete release':'Publish package')+'</button>':'')+'</section>';
    }
    return html;
  };
  U.updateDetails=details;
  document.addEventListener('click',async event=>{
    const paging=event.target.closest('[data-update-page]');
    if(paging){uploadOffset=Math.max(0,uploadOffset+(paging.dataset.updatePage==='next'?50:-50));U.navigate();return;}
    const publish=event.target.closest('[data-publish-update]');
    if(publish){
      publish.disabled=true;
      try{await U.api('updates/uploads/'+publish.dataset.publishUpdate+'/publish',{method:'POST',body:{}});document.querySelector('#detail-dialog').close();await U.navigate();U.status('Published successfully.');}
      catch(error){U.status(error.message,true);const node=document.createElement('p');node.className='notice error';node.textContent=error.message;document.querySelector('.dialog-body')?.prepend(node);publish.disabled=false;}
      return;
    }
    const button=event.target.closest('[data-update-id], [data-released-file]');if(!button)return;
    try {
      let item;
      if(button.dataset.updateId)item=await U.api('updates/uploads/'+button.dataset.updateId);
      else item=released(snapshot).find(item=>item.filename===button.dataset.releasedFile);
      if(item)U.open(title({...item,id:item.id||''}),details(item));
    } catch(error){U.status(error.message,true);}
  });
  document.addEventListener('change',event=>{if(event.target.id==='upload-kind')document.querySelector('#update-file').accept=event.target.value==='package'?'.R4U,.r4u':'.JSON,.json';});
  document.addEventListener('submit',event=>{
    if(event.target.id!=='update-upload')return;event.preventDefault();if(U.busy)return;
    const form=event.target,file=form.elements.file.files[0];if(!file)return;
    const kind=form.elements.kind.value,button=form.querySelector('[type="submit"]'),cancel=form.querySelector('#cancel-upload'),progress=form.querySelector('#upload-progress'),caption=form.querySelector('#upload-caption'),message=form.querySelector('#upload-error');
    const generation=U.generation;
    const xhr=new XMLHttpRequest();U.busy=true;button.disabled=true;cancel.hidden=false;progress.hidden=false;message.textContent='';
    xhr.open('PUT','/api/updates/uploads/'+kind);xhr.setRequestHeader('Content-Type',kind==='package'?'application/octet-stream':'application/json');xhr.setRequestHeader('X-R4OS-Request','1');xhr.withCredentials=true;
    xhr.upload.onprogress=event=>{if(event.lengthComputable){progress.value=event.loaded/event.total*100;caption.textContent=Math.round(progress.value)+' % uploaded';U.status('Transfer: '+Math.round(progress.value)+' %');}};
    xhr.onload=async()=>{try{const result=JSON.parse(xhr.responseText);if(xhr.status<200 || xhr.status>=300)throw new Error(result.detail || ({413:'The file is too large.',415:'This file type is not supported.',401:'Please sign in again.'}[xhr.status]) || 'Transfer failed.');uploadOffset=0;if(U.generation===generation)await U.navigate();U.status('File received. Check its validation status in Incoming.');}catch(error){message.textContent=error.message;U.status(error.message,true);}};
    xhr.onerror=()=>{message.textContent='Connection interrupted. Refresh incoming uploads.';U.status(message.textContent,true);};
    xhr.onabort=()=>{U.status(progress.value>=100?'Request cancelled. Check incoming uploads for completely received files.':'Transfer cancelled.');setTimeout(()=>{if(U.generation===generation)U.navigate();},200);};
    xhr.upload.onloadstart=()=>U.jobs?.refresh();
    xhr.onloadend=()=>{U.busy=false;button.disabled=false;cancel.hidden=true;U.jobs?.refresh();};
    cancel.onclick=()=>xhr.abort();xhr.send(file);
  });
})();
