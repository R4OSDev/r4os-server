"use strict";
(() => {
  const U=window.R4,B=U.bench,E=U.esc;
  const colors=['#9ed9ba','#8bbbd1','#d7ba87','#b6a4d8','#d89e9b','#82c8c6','#b9c68d','#9eafce'];
  const prefs={spectra:{area:'overall',axis:'time',refs:false},snes:{metric:'realtime_factor'}};
  const metricNames={realtime_factor:'Real-time factor',wall_ns:'Wall time (ms)',maximum_pending_master_cycles:'Maximum lag (master cycles)',dropped_presents:'Discarded frame submissions',audio_failures:'Audio errors',audio_discarded_bytes:'PCM discarded on close (bytes)'};
  const panel=(title,html)=>'<section class="panel"><div class="panel-heading"><h2>'+E(title)+'</h2></div>'+html+'</section>';
  const stable=value=>Array.isArray(value)?value.map(stable):value && typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(key=>[key,stable(value[key])])):value;
  const hostName=id=>B.hosts.find(host=>host.host_id===id)?.display_name || id;
  const control=(family,key,label,options,value)=>'<label>'+E(label)+'<select aria-label="'+E(label)+'" data-trend-family="'+family+'" data-trend-key="'+key+'">'+options.map(([id,text])=>'<option value="'+id+'"'+(id===value?' selected':'')+'>'+E(text)+'</option>').join('')+'</select></label>';
  function plot(family,runs){
    const selected=prefs[family];
    const raw=run=>family==='spectra'?B.assessment(run).areas[selected.area].score:['wall_ns','realtime_factor'].includes(selected.metric)?run.validation.computed_summary?.[selected.metric]?.median_value:run.observations?.[selected.metric]?.median_value;
    const value=run=>Number(raw(run))/(family==='snes' && selected.metric==='wall_ns'?1e6:1);
    const points=runs.filter(run=>(family==='spectra'?B.assessment(run).areas[selected.area].status==='valid':run.validation.status==='valid') && raw(run)!==null && raw(run)!==undefined && Number.isFinite(value(run)) && run.started_at_utc).sort((a,b)=>a.started_at_utc.localeCompare(b.started_at_utc) || a.run_id.localeCompare(b.run_id));
    if(!points.length)return U.empty('No valid measurements for this trend','Invalid, incomplete and unknown values remain visible in the lists.');
    const width=1000,height=280,left=76,right=22,top=20,bottom=45;
    const maximum=Math.max(family==='spectra'?120:1,...points.map(value))*1.12;
    const times=points.map(run=>Date.parse(run.started_at_utc)),first=times[0],last=times[times.length-1];
    const indices=new Map(points.map((run,index)=>[run.run_id,index]));
    const equal=family==='spectra' && selected.axis==='measurements';
    const x=run=>points.length===1 || (!equal && first===last)?(width+left-right)/2:left+(equal?indices.get(run.run_id)/(points.length-1):(Date.parse(run.started_at_utc)-first)/(last-first))*(width-left-right);
    const y=number=>height-bottom-number/maximum*(height-top-bottom);
    const groups=new Map();
    for(const run of points){
      const env=run.environment || {};
      const key=family==='spectra'?JSON.stringify(selected.refs?[run.host_id,B.reference(run),run.comparison_key,run.suite_version,run.scoring_version,run.instrumentation_mode]:[run.host_id]):JSON.stringify(stable([run.host_id,run.profile,run.region,run.instrumentation_mode,run.workload,env.execution,env.backend,env.conditions,env.host,env.cpu,env.vcpus,env.ram_bytes,env.accelerator,env.qemu,run.host_snapshot?.hardware]));
      if(!groups.has(key))groups.set(key,[]);groups.get(key).push(run);
    }
    let svg='<svg viewBox="0 0 '+width+' '+height+'" role="img" aria-label="Benchmark performance trend">';
    for(let index=0;index<=4;index++){const number=maximum*index/4;svg+='<line x1="'+left+'" x2="'+(width-right)+'" y1="'+y(number)+'" y2="'+y(number)+'" stroke="#2d3b43" stroke-dasharray="3 5"/><text x="'+(left-10)+'" y="'+(y(number)+4)+'" text-anchor="end">'+E(U.number(number))+'</text>';}
    if(family==='spectra')svg+='<line x1="'+left+'" x2="'+(width-right)+'" y1="'+y(100)+'" y2="'+y(100)+'" stroke="#577b65" stroke-dasharray="7 5"/>';
    const legend=[];let index=0;
    for(const group of groups.values()){
      const color=index%colors.length,firstRun=group[0];index++;
      svg+='<path d="'+group.map((run,i)=>(i?'L':'M')+x(run)+','+y(value(run))).join(' ')+'" fill="none" stroke="'+colors[color]+'" stroke-width="2"/>';
      for(const run of group){const label=hostName(run.host_id)+' · '+U.date(run.started_at_utc)+' · '+String(raw(run))+(family==='spectra'?' points · Reference '+B.reference(run):selected.metric==='wall_ns'?' ns':'');svg+='<circle class="trend-point" cx="'+x(run)+'" cy="'+y(value(run))+'" r="4.5" fill="'+colors[color]+'" tabindex="0" role="button" data-family="'+family+'" data-benchmark-run="'+E(run.run_id)+'" aria-label="'+E(label)+'"><title>'+E(label)+'</title></circle>';}
      const label=family==='spectra'?hostName(firstRun.host_id)+(selected.refs?' · Ref. '+B.reference(firstRun).slice(0,8)+' · '+(firstRun.comparison_key || '—'):''):hostName(firstRun.host_id)+' · '+firstRun.profile+' · '+firstRun.region.toUpperCase()+' · '+firstRun.instrumentation_mode+' · '+(firstRun.environment.backend || '—');
      legend.push('<span><i class="legend-swatch series-'+color+'"></i>'+E(label)+'</span>');
    }
    svg+='<text x="'+left+'" y="'+(height-13)+'">'+E(equal?'Measurement 1':U.date(points[0].started_at_utc))+'</text><text x="'+(width-right)+'" y="'+(height-13)+'" text-anchor="end">'+E(equal?'Measurement '+points.length:U.date(points[points.length-1].started_at_utc))+'</text></svg>';
    return '<div class="trend-chart">'+svg+'</div><div class="trend-legend">'+legend.join('')+'</div>';
  }
  B.trend=(family,runs)=>{
    const p=prefs[family];
    const controls=family==='spectra'?control(family,'area','Index',Object.entries(B.areas),p.area)+control(family,'axis','X axis',[['time','Time axis'],['measurements','Evenly distributed']],p.axis)+'<label class="toggle-control"><input type="checkbox" data-trend-family="spectra" data-trend-key="refs"'+(p.refs?' checked':'')+'>References</label>':control(family,'metric','Metric',Object.entries(metricNames),p.metric);
    const note=family==='spectra'?'Current table page. Server-calculated scores where available; reported scores keep their original reference. '+(p.refs?'Separate curves for each host, reference and comparison profile.':'Curves connect each host across changes in reference and profile.'):'Current table page. Valid runs only, grouped by workload, host/CPU/VM context, region, profile, mode and conditions. Missing values remain unknown.';
    return '<section class="panel" id="benchmark-trend"><div class="panel-heading"><h2>Performance trend</h2><div class="trend-controls">'+controls+'</div></div>'+plot(family,runs)+'<p class="chart-note">'+E(note)+'</p></section>';
  };
  document.addEventListener('change',event=>{
    const control=event.target.closest('[data-trend-key]');if(!control)return;
    const family=control.dataset.trendFamily,key=control.dataset.trendKey;prefs[family][key]=control.type==='checkbox'?control.checked:control.value;
    const container=document.querySelector('#benchmark-trend');if(container){container.outerHTML=B.trend(family,B.states[family].page.items);document.querySelector('[data-trend-family="'+family+'"][data-trend-key="'+key+'"]')?.focus();}
  });
  document.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key) && event.target.matches('.trend-point')){event.preventDefault();B.openRun(event.target.dataset.family,event.target.dataset.benchmarkRun);}});

  const field=(name,label,value='',extra='')=>'<label>'+E(label)+'<input name="'+name+'" value="'+E(value)+'" '+extra+'></label>';
  const cpuModel=host=>typeof host?.hardware?.cpu==='string'?host.hardware.cpu:host?.hardware?.cpu?.model || '';
  const osName=host=>typeof host?.metadata?.operating_system==='string'?host.metadata.operating_system:'';
  const ramMiB=host=>{try{return host?.hardware?.ram_bytes===null || host?.hardware?.ram_bytes===undefined?'':String(BigInt(host.hardware.ram_bytes)/1048576n);}catch{return '';}};
  B.hostCreateButton=()=>'<button class="button primary" data-host-create>Create host</button>';
  B.hostActions=host=>'<div class="form-actions"><button class="button primary" data-host-edit="'+E(host.host_id)+'">Edit host</button></div>';
  function editHost(host=null){
    B.editingHost=host;
    const html='<form id="benchmark-host-editor"><div class="form-grid">'+field('host_id','Host-ID',host?.host_id || '','required maxlength="64" pattern="[a-z0-9][a-z0-9_-]{0,63}" '+(host?'readonly':''))+field('display_name','Display name',host?.display_name || '','required maxlength="128"')+field('cpu_model','CPU model',cpuModel(host),'maxlength="256"')+field('logical_cpus','Logical CPUs',host?.hardware?.logical_cpus ?? '','inputmode="numeric" pattern="[0-9]+"')+field('ram_mib','RAM (MiB)',ramMiB(host),'inputmode="numeric" pattern="[0-9]+"')+field('operating_system','Host operating system',osName(host))+'</div><label class="spaced-field">Description<textarea name="description" rows="3" maxlength="4096">'+E(host?.description || '')+'</textarea></label>'+field('tags','Tags, separated by commas',host?.tags.join(', ') || '')+'<p class="muted">The host ID remains stable. Changes apply to the host record; existing runs keep their original snapshot.</p><div class="form-actions"><button class="button primary" type="submit">Save host</button></div><p class="form-message" role="alert"></p></form>';
    U.open(host?'Edit host':'Create host',html);
  }
  document.addEventListener('submit',async event=>{
    const form=event.target;if(form.id!=='benchmark-host-editor')return;event.preventDefault();
    const fields=form.elements,previous=B.editingHost,identifier=previous?.host_id || fields.host_id.value.trim();
    const message=form.querySelector('.form-message'),button=form.querySelector('[type="submit"]');button.disabled=true;
    try{
      if(!previous && B.hosts.some(host=>host.host_id===identifier))throw new Error('This host ID already exists. Open the existing host to edit it.');
      const hardware=structuredClone(previous?.hardware || {}),metadata=structuredClone(previous?.metadata || {});
      const model=fields.cpu_model.value.trim();
      if(model!==String(cpuModel(previous))){
        if(model)hardware.cpu={...(hardware.cpu && typeof hardware.cpu==='object'?hardware.cpu:{}),model};
        else if(hardware.cpu && typeof hardware.cpu==='object')delete hardware.cpu.model;else delete hardware.cpu;
      }
      const cpus=fields.logical_cpus.value.trim(),ram=fields.ram_mib.value.trim();
      if(cpus!==String(previous?.hardware?.logical_cpus ?? '')){if(cpus)hardware.logical_cpus=String(BigInt(cpus));else delete hardware.logical_cpus;}
      if(ram!==ramMiB(previous)){if(ram)hardware.ram_bytes=String(BigInt(ram)*1048576n);else delete hardware.ram_bytes;}
      const operatingSystem=fields.operating_system.value.trim();
      if(operatingSystem!==osName(previous)){if(operatingSystem)metadata.operating_system=operatingSystem;else delete metadata.operating_system;}
      const tags=fields.tags.value===previous?.tags.join(', ')?previous.tags:fields.tags.value.split(',').map(value=>value.trim()).filter(Boolean);
      await U.api('benchmarks/hosts/'+encodeURIComponent(identifier),{method:'PUT',body:{host_id:identifier,display_name:fields.display_name.value.trim(),description:fields.description.value.trim() || null,hardware,metadata,tags}});
      document.querySelector('#detail-dialog').close();await U.navigate();U.status('Host record saved.');
    }catch(error){message.textContent=error instanceof SyntaxError?'CPU count and RAM must be non-negative integers.':error.message;U.status(message.textContent,true);button.disabled=false;}
  });
  document.addEventListener('click',async event=>{
    if(event.target.closest('[data-host-create]')){editHost();return;}
    const edit=event.target.closest('[data-host-edit]');if(edit){try{editHost(await U.api('benchmarks/hosts/'+encodeURIComponent(edit.dataset.hostEdit)));}catch(error){U.status(error.message,true);}}
  });

  const runLink=(family,id,label)=>'<a class="button subtle" href="#benchmarks/'+family+'/'+encodeURIComponent('run:'+id)+'">'+E(label)+'</a>';
  B.detailActions=(family,run)=>'<div class="dialog-toolbar"><button class="button" data-benchmark-export="'+family+'" data-run-id="'+E(run.run_id)+'">Export JSON</button>'+runLink(family,run.run_id,'Direct link')+(family==='spectra' && B.reference(run)?runLink(family,B.reference(run),'Open reference'):'')+'</div>';
  B.detailExtra=(family,run)=>family==='snes'?panel('Compare runs','<form id="snes-comparison-form" class="panel-body"><div class="comparison-controls"><label>Reference run (UUID)<input name="reference_run_id" required maxlength="36" placeholder="Full run UUID" list="snes-reference-options"></label><datalist id="snes-reference-options">'+(B.states.snes.page?.items || []).filter(value=>value.run_id!==run.run_id).map(value=>'<option value="'+E(value.run_id)+'">'+E(hostName(value.host_id)+' / '+value.profile+' / '+value.region)+'</option>').join('')+'</datalist><button class="button primary" type="submit">Compare</button></div><p class="muted">Choose the reference explicitly. A direct performance comparison requires matching workload, region, profile, mode and conditions.</p><div id="snes-comparison-result" aria-live="polite"></div></form>'):'';
  let compareGeneration=0;
  document.addEventListener('submit',async event=>{
    const form=event.target;if(form.id!=='snes-comparison-form')return;event.preventDefault();
    const selected=B.selection;if(!selected || selected.family!=='snes')return;
    const reference=form.elements.reference_run_id.value.trim(),result=form.querySelector('#snes-comparison-result'),button=form.querySelector('button');
    const generation=++compareGeneration;button.disabled=true;result.innerHTML='<p class="muted">Checking comparison …</p>';
    try{
      const [comparison,previous]=await Promise.all([U.api('benchmarks/subsystem-benchmark-runs/'+selected.run.run_id+'/comparison?reference_run_id='+encodeURIComponent(reference)),U.api('benchmarks/subsystem-benchmark-runs/'+encodeURIComponent(reference))]);
      if(generation!==compareGeneration || B.selection!==selected || !document.querySelector('#detail-dialog').open || !result.isConnected)return;
      const context=run=>{const env=run.environment,processor=env.cpu?.model || env.host?.cpu?.model || env.host?.cpu || run.host_snapshot?.hardware?.cpu?.model || 'Unknown CPU';return hostName(run.host_id)+' / '+(typeof processor==='string'?processor:'CPU context in snapshot')+' / '+env.execution+' / R4OS '+(run.versions.release_version || '—')+' / R4SNES '+(run.versions.emulator_version || '—');};
      let html='<p class="muted">Current: '+E(context(selected.run))+'<br>Reference: '+E(context(previous))+'<br><code>'+E(reference)+'</code></p>'+runLink('snes',reference,'Open reference');
      if(!comparison.compatible)html+='<p class="notice">No direct performance comparison: '+E(comparison.reasons.map(B.reason).join(' · '))+'</p>';
      else {
        html+='<p class="muted">Matching workload and measurement conditions. Absolute medians without hardware normalisation or a point score.'+(comparison.different_hosts?' Different hosts remain part of the comparison.':'')+'</p>';
        html+=U.table(['Metric','Up to date','Reference','Ratio','Change'],Object.entries(comparison.metrics).map(([name,value])=>[name==='wall_ns'?'Wall time (ns) · lower is better':'Real-time factor · higher is better','<code>'+E(value.value)+'</code>','<code>'+E(value.reference_value)+'</code>',E(U.number(value.ratio))+' ×',E(U.number(value.change_percent))+' %']));
      }
      result.innerHTML=html;
    }catch(error){if(generation!==compareGeneration || B.selection!==selected || !document.querySelector('#detail-dialog').open || !result.isConnected)return;result.innerHTML='<p class="notice error">'+E(error.status===404?'Reference run not found.':error.message)+'</p>';U.status(error.message,true);}
    finally{button.disabled=false;}
  });
  document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-benchmark-export]');if(!button)return;button.disabled=true;
    try{
      const family=button.dataset.benchmarkExport,collection=family==='spectra'?'benchmark-runs':'subsystem-benchmark-runs';
      const response=await fetch('/api/benchmarks/'+collection+'/'+encodeURIComponent(button.dataset.runId),{cache:'no-store',credentials:'same-origin'});
      if(!response.ok)throw new Error('The export could not be loaded.');
      const blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');
      link.href=url;link.download=family+'-'+button.dataset.runId+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);U.status('Full JSON export ready.');
    }catch(error){U.status(error.message,true);}
    finally{button.disabled=false;}
  });
})();
