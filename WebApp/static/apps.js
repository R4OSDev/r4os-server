"use strict";
(() => {
  const U=window.R4,E=U.esc,api=(path,options)=>U.api('apps/'+path,options);
  let editing=null;
  U.views.apps=async ctx=>{
    U.side(ctx,[['overview','Overview']]);
    const value=await api('overview');if(ctx.generation!==U.generation)return;
    const rows=value.apps.map(app=>U.record(
      '<div class="record-grid app-overview-grid"><div class="record-identity"><h2 class="record-name app-name">'+E(app.display_name)+'</h2><span class="record-subline app-version">Version '+E(app.version)+'</span>'+(app.description?'<p class="app-description">'+E(app.description)+'</p>':'')+'</div>'+
      '<div role="group" aria-label="Install-Path"><span class="app-install-path">'+E(app.install_path)+'</span></div>'+
      '<div role="group" aria-label="Source">'+E(app.owner)+'<small class="cell-subline">'+E(app.repository_name)+'</small>'+(!app.source_available?'<p class="muted">'+E(app.source_error||'Source unavailable')+'</p>':'')+'</div><span class="badge app-visibility" title="Source repository visibility">'+(app.private?'Private':'Public')+'</span></div>',
      '<button type="button" class="button subtle" data-edit-app="'+E(app.repository)+'" aria-label="Edit '+E(app.display_name)+'">Edit</button>',
      ' data-app-repository="'+E(app.repository)+'"'
    ));
    const html=rows.length?'<section class="apps-list" aria-label="Apps overview">'+U.records(rows,'Apps')+'</section>':value.publishing?U.empty('Adding apps','Apps appear here after their manifests have been read.'):U.empty('No apps listed','Enable Show in Apps for an application in Repositories → Overview.');
    U.page(ctx,'Apps','',html);U.status('Apps loaded.');
    if(value.publishing)poll(ctx);
  };
  function poll(ctx){U.poll=setTimeout(()=>{if(ctx.generation!==U.generation)return;if(document.querySelector('#detail-dialog[open]'))poll(ctx);else U.navigate();},1500);}
  document.addEventListener('click',async event=>{
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
      if(generation===U.generation&&form.isConnected){await U.navigate();U.status('App settings saved.');}
    }catch(error){if(form.isConnected)form.querySelector('.form-message').textContent=error.message;U.status(error.message,true);}
    finally{if(submit.isConnected)submit.disabled=false;}
  });
})();
