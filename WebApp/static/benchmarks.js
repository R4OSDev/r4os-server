"use strict";
(() => {
  const U=window.R4,E=U.esc;
  const areas={overall:'Overall index',scheduler_sync:'Scheduler / Sync',memory:'Memory',ipc:'Kernel communication',lifecycle:'Program lifecycle',filesystem_io:'File / I/O'};
  const shortAreas={scheduler_sync:'Sync',memory:'RAM',ipc:'IPC',lifecycle:'Start',filesystem_io:'I/O'};
  const states={spectra:{filters:{},offset:0,leaf:null},snes:{filters:{},offset:0,leaf:null}};
  const B=U.bench={areas,states,hosts:[],selection:null,detailGeneration:0,sampleGeneration:0};
  const profiles={'core-throughput':'Core throughput','window-audio':'Window / audio'};
  const modes={normal:'Normal',detail:'Detailed diagnostics'};
  const statuses=['completed','failed','incomplete','cancelled','running'].map(value=>[value,U.labels[value]]);
  const reasonNames={no_baseline:'No reference selected',unknown_baseline:'Reference not received yet',baseline_not_completed:'Reference run not completed',run_not_completed:'Run not completed',five_valid_areas_required:'Five valid areas are required',reported_score_mismatch:'Reported score differs from the calculation',baseline_missing_primary_metrics:'The reference is missing primary metrics',primary_metric_set_mismatch:'Primary metrics differ',comparison_mismatch:'Comparison profiles differ',environment_incomplete:'Environment details missing',environment_invalid:'Environment does not meet the measurement contract',environment_mismatch:'Measurement conditions differ',metric_contract_mismatch:'Metric contracts differ',invalid_primary_metric:'Invalid primary metric',invalid_or_incomplete_run:'At least one run is invalid or incomplete',different_measurement_conditions:'Measurement conditions differ',different_execution_profile:'Execution profiles differ',different_workload:'Fixed workload or inputs differ',different_region:'Regions differ',different_profile:'Profilees differ',different_instrumentation_mode:'Measurement modes differ',five_complete_batches_required:'Five complete batches are required',run_failed:'Run failed',run_incomplete:'Run incomplete'};
  Object.assign(reasonNames,{run_failure_reported:'The run reports a failure',standard_metric_checks_incomplete:'Clock, work and cleanup checks must be confirmed',standard_raw_samples_incomplete:'Raw measurements do not match the declared repetitions',standard_warmup_missing:'Required warm-up measurements are missing',standard_raw_sample_invalid:'Raw measurements or CPU completion are invalid',standard_metric_summary_mismatch:'Metric summaries do not match the raw samples',server_baseline_unavailable:'No eligible server reference is available',unsupported_automatic_scoring_contract:'Automatic server scoring is unavailable for this measurement contract'});
  B.reason=value=>{if(!value)return '—';if(value==='metric_contract_mismatch:suite3_primary_metric_version')return 'Suite 3 requires primary metric version 2';const [prefix,...rest]=value.split(':');return (reasonNames[prefix] || prefix)+(rest.length?' · '+rest.join(':'):'');};
  const panel=(title,html)=>'<section class="panel"><div class="panel-heading"><h2>'+E(title)+'</h2></div>'+html+'</section>';
  const hostName=id=>B.hosts.find(host=>host.host_id===id)?.display_name || id;
  const cpu=environment=>{const value=environment?.cpu?.model || environment?.host?.cpu?.model || environment?.host?.cpu;return typeof value==='string'?value:'—';};
  const optionValues=values=>(values||[]).map(value=>[value,value]);
  const select=(name,label,options,value='',empty='All')=>'<label>'+E(label)+'<select name="'+E(name)+'"><option value="">'+E(empty)+'</option>'+options.map(([id,text])=>'<option value="'+E(id)+'"'+(String(id)===String(value)?' selected':'')+'>'+E(text)+'</option>').join('')+'</select></label>';
  const input=(name,label,value='',type='text')=>'<label>'+E(label)+'<input name="'+E(name)+'" type="'+E(type)+'" value="'+E(value)+'"></label>';
  B.assessment=run=>run.server_scoring?.applicable?run.server_scoring:run.score_validation;
  B.reference=run=>run.server_scoring?.applicable?run.server_scoring.baseline_run_id:run.baseline_run_id;
  const assessmentBadge=run=>run.server_scoring?.applicable?'<span class="muted">Server</span> '+U.badge(run.server_scoring.status)+' <span class="muted">Reported</span> '+U.badge(run.score_validation.status):U.badge(run.score_validation.status);
  const score=(run,area)=>{const value=B.assessment(run)?.areas?.[area];return value?.score===null || !value?'—':'<span class="score-value '+(value.status==='valid'?'':'score-unqualified')+'" title="'+E(value.score+' · '+B.reason(value.reason))+'">'+E(U.number(value.score))+'</span>';};
  const runButton=(family,run,text)=>'<button class="text-button" data-benchmark-run="'+E(run.run_id)+'" data-family="'+family+'">'+E(text)+'</button>';
  const paging=(family,page)=>'<div class="pagination"><span>'+E(U.count(page.total))+' runs · '+(page.total?E(U.count(page.offset+1))+'–'+E(U.count(page.offset+page.items.length)):'0')+'</span><div><button class="button subtle" data-benchmark-page="previous" data-family="'+family+'"'+(page.offset===0?' disabled':'')+'>Previous</button><button class="button subtle" data-benchmark-page="next" data-family="'+family+'"'+(page.offset+page.items.length>=page.total?' disabled':'')+'>Next</button></div></div>';
  const hostOptions=()=>B.hosts.map(host=>[host.host_id,host.display_name+' / '+host.host_id]);
  function filters(family,inventory={}){
    const f=states[family].filters;
    let basic=select('host_id','Host',hostOptions(),f.host_id);
    let advanced='';
    if(family==='spectra'){
      basic+=select('release_version','R4OS version',optionValues(inventory.versions?.release_version),f.release_version)+select('status','Run status',statuses,f.status);
      advanced+=select('environment_id','Environment',(inventory.environments||[]).map(value=>[value.environment_id,value.environment_id]),f.environment_id);
      for(const [key,label] of [['kernel_version','Kernel version'],['spectra_version','Spectra version'],['suite_version','Suite'],['scoring_version','Scoring version']])advanced+=select(key,label,optionValues(inventory.versions?.[key]),f[key]);
      advanced+=select('server_baseline_run_id','Server reference',(inventory.server_baselines||[]).map(value=>[value.baseline_run_id,(value.host_name || 'Unknown host')+' · '+value.baseline_run_id.slice(0,8)]),f.server_baseline_run_id);
      advanced+=select('baseline_run_id','Reported reference',(inventory.baselines||[]).map(value=>[value.baseline_run_id,(value.host_name || 'Unknown host')+' · '+value.baseline_run_id.slice(0,8)]),f.baseline_run_id);
      advanced+=input('comparison_key','Comparison profile',f.comparison_key)+input('instrumentation_mode','Measurement mode',f.instrumentation_mode);
    }else{
      basic+=select('profile','Profile',Object.entries(profiles),f.profile)+select('region','Region',[['ntsc','NTSC'],['pal','PAL']],f.region)+select('instrumentation_mode','Measurement mode',Object.entries(modes),f.instrumentation_mode)+select('status','Run status',statuses,f.status);
      for(const [key,label] of [['scene_id','Scene'],['rom_sha256','ROM SHA-256'],['emulator_version','R4SNES version'],['release_version','R4OS version'],['backend','Backend']])advanced+=input(key,label,f[key]);
    }
    basic+=input('date_from','From (UTC)',f.date_from,'date')+input('date_to','To (UTC)',f.date_to,'date');
    return panel('Selection','<form id="benchmark-filters" data-family="'+family+'" class="panel-body"><div class="filter-grid">'+basic+'</div><details class="advanced-filters"><summary>More filters</summary><div class="filter-grid">'+advanced+'</div></details><div class="form-actions"><button class="button primary" type="submit">Filter</button><button class="button subtle" type="button" data-benchmark-reset="'+family+'">Reset</button></div></form>');
  }

  U.views.benchmarks=async ctx=>{
    const family=['spectra','snes','hosts'].includes(ctx.sub)?ctx.sub:'spectra';ctx.sub=family;
    U.side(ctx,[['spectra','Spectra'],['snes','SNES'],['hosts','Hosts']]);
    const hosts=await U.api('benchmarks/hosts');if(ctx.generation!==U.generation)return;B.hosts=hosts.items;
    if(family==='hosts'){
      U.context(ctx,[['all','All hosts'],...B.hosts.map(host=>['host:'+host.host_id,host.display_name])]);
      const items=ctx.leaf.startsWith('host:')?B.hosts.filter(host=>host.host_id===ctx.leaf.slice(5)):B.hosts;
      const rows=items.map(host=>['<button class="text-button" data-benchmark-host="'+E(host.host_id)+'">'+E(host.display_name)+'</button><small class="cell-subline">'+E(host.host_id)+'</small>',E(host.hardware.cpu?.model || (typeof host.hardware.cpu==='string'?host.hardware.cpu:'—')),E(U.count(host.hardware.logical_cpus)),'<span title="'+E(host.hardware.ram_bytes || '')+'">'+E(U.bytes(host.hardware.ram_bytes))+'</span>',E(U.count(host.run_count))]);
      U.page(ctx,'Hosts','Stable identities for benchmark hosts. Saved runs retain their historical snapshots.',panel('Host records',rows.length?U.table(['Host','CPU','Logical CPUs','RAM','Runs'],rows):U.empty('No hosts yet','Create a benchmark host before the runner submits its first run.')),B.hostCreateButton?B.hostCreateButton():'');
      U.status('Host registry loaded.');return;
    }
    const state=states[family];
    if(state.leaf!==ctx.leaf && (ctx.leaf==='all' || ctx.leaf.startsWith('host:'))){state.filters.host_id=ctx.leaf==='all'?'':ctx.leaf.slice(5);state.offset=0;state.leaf=ctx.leaf;}
    U.context({...ctx,leaf:state.filters.host_id?'host:'+state.filters.host_id:'all'},[['all','All hosts'],...B.hosts.map(host=>['host:'+host.host_id,host.display_name])]);
    const query=new URLSearchParams(Object.entries(state.filters).filter(([,value])=>value));query.set('limit','50');query.set('offset',String(state.offset));
    const collection=family==='spectra'?'benchmark-runs':'subsystem-benchmark-runs';
    const [page,inventory]=await Promise.all([U.api('benchmarks/'+collection+'?'+query),family==='spectra'?U.api('benchmarks/benchmark-filters'):Promise.resolve({})]);
    if(ctx.generation!==U.generation)return;state.page=page;
    let rows,headers,description;
    if(family==='spectra'){
      description='Server-calculated indices use the first valid compatible reference. Reference = 100 points; reported scores retain their original reference.';
      headers=['Measurement (UTC)','Host / version','Overall index','Areas','Run / score'];
      rows=page.items.map(run=>[runButton(family,run,U.date(run.started_at_utc))+'<small class="cell-subline">'+E(run.run_id.slice(0,8))+'</small>',E(run.host_name || hostName(run.host_id))+'<small class="cell-subline">R4OS '+E(run.release_version || '—')+' · '+E(run.suite_version || '—')+'</small>',score(run,'overall'),'<div class="area-minis">'+Object.entries(shortAreas).map(([area,label])=>'<span title="'+E(areas[area])+'"><small>'+label+'</small>'+score(run,area)+'</span>').join('')+'</div>',U.badge(run.status)+' '+assessmentBadge(run)]);
    }else{
      description='Fixed guest workload, separate execution profiles and direct measurements. No SNES point score.';
      headers=['Measurement (UTC)','Host / profile','Scene / version','Wall time / real-time factor','Run / validation'];
      rows=page.items.map(run=>{
        const valid=run.validation.status==='valid',summary=run.validation.computed_summary;
        return [runButton(family,run,U.date(run.started_at_utc))+'<small class="cell-subline">'+E(run.run_id.slice(0,8))+'</small>',E(hostName(run.host_id))+'<small class="cell-subline">'+E(profiles[run.profile]+' · '+run.region.toUpperCase()+' · '+modes[run.instrumentation_mode])+'</small>',E(run.workload.scene_id || '—')+'<small class="cell-subline">R4SNES '+E(run.versions.emulator_version || '—')+' · R4OS '+E(run.versions.release_version || '—')+'</small>',valid?'<span title="'+E(summary.wall_ns.median_value)+' ns">'+E(U.number(Number(summary.wall_ns.median_value)/1e6))+' ms</span><small class="cell-subline">'+E(U.number(summary.realtime_factor.median_value))+' × real time</small>':'—',U.badge(run.status)+' '+U.badge(run.validation.status)];
      });
    }
    let html=filters(family,inventory);
    if(B.trend)html+=B.trend(family,page.items);
    html+=panel(family==='spectra'?'Spectra runs':'SNES runs',rows.length?U.table(headers,rows)+paging(family,page):U.empty('No matching runs','Change the filters or submit a real benchmark run through the runner.'));
    U.page(ctx,family==='spectra'?'Spectra':'SNES',description,html);U.status(U.count(page.total)+' benchmark runs found.');
    if(ctx.leaf.startsWith('run:'))await openRun(family,ctx.leaf.slice(4));
  };

  document.addEventListener('submit',event=>{
    if(event.target.id!=='benchmark-filters')return;event.preventDefault();
    const family=event.target.dataset.family,state=states[family];state.filters=Object.fromEntries(new FormData(event.target));state.offset=0;
    const leaf=state.filters.host_id?'host:'+state.filters.host_id:'all';state.leaf=leaf;
    const hash='#benchmarks/'+family+'/'+encodeURIComponent(leaf);if(location.hash===hash)U.navigate();else location.hash=hash;
  });
  document.addEventListener('click',async event=>{
    const reset=event.target.closest('[data-benchmark-reset]');if(reset){const family=reset.dataset.benchmarkReset;states[family].filters={};states[family].offset=0;states[family].leaf='all';const hash='#benchmarks/'+family+'/all';if(location.hash===hash)U.navigate();else location.hash=hash;return;}
    const page=event.target.closest('[data-benchmark-page]');if(page){const state=states[page.dataset.family];state.offset=Math.max(0,state.offset+(page.dataset.benchmarkPage==='next'?50:-50));U.navigate();return;}
    const run=event.target.closest('[data-benchmark-run]');if(run){await openRun(run.dataset.family,run.dataset.benchmarkRun);return;}
    const host=event.target.closest('[data-benchmark-host]');if(host){try{const data=await U.api('benchmarks/hosts/'+encodeURIComponent(host.dataset.benchmarkHost));U.open(data.display_name,hostDetails(data));}catch(error){U.status(error.message,true);}}
  });

  const properties=items=>'<dl class="property-grid">'+items.map(([label,value])=>'<div><dt>'+E(label)+'</dt><dd>'+E(value===null || value===undefined?'—':value)+'</dd></div>').join('')+'</dl>';
  const exact=value=>'<span title="'+E(value ?? '')+'">'+E(U.count(value))+'</span>';
  function hostDetails(host){
    return (host.description?'<p class="detail-description">'+E(host.description)+'</p>':'')+properties([['Host-ID',host.host_id],['Display name',host.display_name],['Runs',U.count(host.run_count)],['Modified',U.date(host.updated_at_utc)]])+U.fold('Hardware',host.hardware)+U.fold('Metadata',host.metadata)+U.fold('Tags',host.tags)+(B.hostActions?B.hostActions(host):'');
  }
  function spectraDetails(run){
    let html='<div class="detail-status">'+U.badge(run.status)+' '+assessmentBadge(run)+'<code>'+E(run.run_id)+'</code></div>';
    if(run.failure_reason)html+='<p class="notice error">'+E(run.failure_reason)+'</p>';
    const computed=B.assessment(run),comparison=computed.comparison;
    html+=properties([['Host at import',run.host_snapshot.display_name+' / '+run.host_id],['R4OS / Kernel / Spectra',[run.release_version,run.kernel_version,run.spectra_version].map(value=>value || '—').join(' / ')],['Suite / Scoring',String(run.suite_version)+' / '+String(run.scoring_version)],['Start / end',U.date(run.started_at_utc)+' / '+U.date(run.finished_at_utc)],['CPU / visible CPUs',cpu(run.environment)+' / '+U.count(run.environment.vcpus)],['Environment',run.environment_id]]);
    const reference=B.reference(run);
    html+='<p class="muted">'+(run.server_scoring?.applicable?'Server reference: ':'Reference: ')+(reference?runButton('spectra',{run_id:reference},reference):'No reference')+'. '+E(comparison.reason?B.reason(comparison.reason):'Measurement contracts are comparable.')+(comparison.cross_host?' Different hosts.':'')+(comparison.cpu_changed?' CPU details differ.':'')+'</p>';
    if(run.server_scoring?.applicable)html+='<p class="muted">Reported reference: '+(run.baseline_run_id?runButton('spectra',{run_id:run.baseline_run_id},run.baseline_run_id):'No reference')+'. Server scores are recalculated from stored measurements; reported scores keep their original reference.</p>';
    html+=panel('Area and overall assessment',U.table(['Area','Server calculated','Calculation','Reported','Reported check','Reason'],Object.entries(areas).map(([area,label])=>{const value=computed.areas[area],reported=run.score_validation.areas[area],hasReport=reported.reported_score!==null;const reasons=[...new Set([value.reason,hasReport?reported.reason:null].filter(Boolean))];return [E(label),'<span title="'+E(value.score ?? '')+'">'+E(U.number(value.score))+'</span>',U.badge(value.status),'<span title="'+E(reported.reported_score ?? '')+'">'+E(U.number(reported.reported_score))+'</span>',hasReport?U.badge(reported.status):'Not provided',E(reasons.map(B.reason).join('; '))];})));
    html+='<p class="muted">Calculated from the submitted metric aggregates. Differences in hosts, hardware and CPU counts remain part of the measurement context.</p>';
    html+=panel('Metrics',run.metrics.length?U.table(['Area / metric','Version / unit','Status','Median','Min / Max','MAD','Samples / weight'],run.metrics.map(metric=>[E(areas[metric.area_id])+'<small class="cell-subline">'+E(metric.metric_id)+(metric.primary?' · Primary metric':'')+'</small>',E(metric.metric_version || '—')+' / '+E(metric.unit || '—'),U.badge(metric.status),'<code>'+E(metric.median_value ?? '—')+'</code>','<code>'+E(metric.min_value ?? '—')+' / '+E(metric.max_value ?? '—')+'</code>','<code>'+E(metric.mad_value ?? '—')+'</code>',E(U.count(metric.sample_count))+' / '+E(metric.weight ?? '—')])):U.empty('No metrics available'));
    html+=panel('Raw samples · '+U.count(run.sample_total),'<div class="panel-body"><div class="form-grid">'+select('metric','Metric',run.metrics.map(metric=>[JSON.stringify([metric.area_id,metric.metric_id]),areas[metric.area_id]+' / '+metric.metric_id]),'','All metrics').replace('name="metric"','name="metric" id="sample-metric"')+select('phase','Phase',[['warmup','Warm-up'],['measurement','Measurement']],'','All phases').replace('name="phase"','name="phase" id="sample-phase"')+'</div></div><div id="benchmark-samples"></div>');
    html+=U.fold('Environment snapshot',run.environment)+U.fold('Host snapshot at import',run.host_snapshot)+U.fold('Artefacts, sources and hashes',run.artifacts)+U.fold('Metric checks and reported scores',{metrics:run.metrics,scores:run.scores});
    if(run.notes)html+='<p class="detail-description">'+E(run.notes)+'</p>';
    return html;
  }
  function median(values){
    if(!values.length || values.some(value=>value===null || value===undefined))return null;
    try{const sorted=values.map(BigInt).sort((a,b)=>a<b?-1:a>b?1:0),middle=Math.floor(sorted.length/2);if(sorted.length%2)return String(sorted[middle]);const sum=sorted[middle-1]+sorted[middle];return String(sum/2n)+(sum%2n?'.5':'');}catch{return null;}
  }
  function snesDetails(run){
    const validation=run.validation,summary=validation.computed_summary;
    let html='<div class="detail-status">'+U.badge(run.status)+' '+U.badge(validation.status)+'<code>'+E(run.run_id)+'</code></div>';
    if(run.failure_reason || validation.reasons.length)html+='<p class="notice'+(validation.status==='valid'?'':' error')+'">'+E(run.failure_reason || validation.reasons.map(B.reason).join(' · '))+'</p>';
    html+=properties([['Host / execution',run.host_id+' / '+run.environment.execution],['Profile',profiles[run.profile]],['Region / measurement mode',run.region.toUpperCase()+' / '+modes[run.instrumentation_mode]],['R4OS / Kernel / R4SNES',[run.versions.release_version,run.versions.kernel_version,run.versions.emulator_version].map(value=>value || '—').join(' / ')],['Start / end',U.date(run.started_at_utc)+' / '+U.date(run.finished_at_utc)],['CPU',cpu(run.environment)],['Scene / workload',(run.workload.scene_id || '—')+' / '+(run.workload.work_version || '—')],['ROM SHA-256',run.workload.rom_sha256 || '—']]);
    html+=panel('Calculated summary',U.table(['Metric','Median','Min','Max','MAD'],['wall_ns','realtime_factor'].map(metric=>[metric==='wall_ns'?'Wall time (ns)':'Real-time factor',...['median_value','min_value','max_value','mad_value'].map(key=>'<code>'+E(summary?.[metric]?.[key] ?? '—')+'</code>')])));
    const phases=['CPU / Bus','DMA','PPU','SMP / SPC700','DSP','Coprocessor'];
    html+=panel('Sampled core phases',U.table(['Phase','Median wall time (ns)','Median calls'],phases.map((label,index)=>[label,exact(median(run.samples.map(sample=>sample.sampled_exclusive_wall_ns?.[index]))),exact(median(run.samples.map(sample=>sample.sampled_phase_calls?.[index])))]))+'<p class="panel-body muted">Exclusive wall time of the operations actually sampled. These are not CPU percentages or extrapolations to the whole batch. Detailed timings remain unknown in normal mode.</p>');
    html+=panel('Warm-up runs',U.table(['Batch','Status','Guest time (ns)','Wall time (ns)','Master cycles','Frames / APU'],run.samples.map(sample=>[exact(sample.sample),U.badge(sample.warmup?.status || 'unavailable'),exact(sample.warmup?.guest_ns),exact(sample.warmup?.wall_ns),exact(sample.warmup?.master_cycles),exact(sample.warmup?.generated_frames)+' / '+exact(sample.warmup?.apu_native_frames)])));
    html+=panel('Measurement batches',U.table(['Batch','Status','Wall time (ns)','Thread-CPU (ns)','Owner ticks / Hz','Master start → end','Master Hz','Cleanup'],run.samples.map(sample=>[exact(sample.sample),U.badge(sample.status || 'unavailable'),exact(sample.wall_ns),exact(sample.thread_cpu_ns),exact(sample.assigned_runtime_ticks)+' / '+exact(sample.runtime_tick_hz),exact(sample.work_start_master)+' → '+exact(sample.work_end_master),exact(sample.master_hz),sample.cleanup_verified===true?'Confirmed':'Not confirmed'])));
    html+='<p class="muted">Wall time includes waiting and preemption; the window profile also includes the final audio close/drain. Owner ticks exclude the desktop and audio service. Execution profiles do not isolate their costs by subtraction.</p>';
    html+=panel('Video and audio',U.table(['Batch','Frames generated','Submitted','Visibly confirmed','Discarded','APU frames','PCM accepted','Core PCM','Playback frames','Audio errors','PCM discarded'],run.samples.map(sample=>[exact(sample.sample),exact(sample.generated_frames),exact(sample.submitted_frames),exact(sample.actually_presented_frames),exact(sample.dropped_presents),exact(sample.apu_native_frames),exact(sample.audio_accepted_bytes),exact(sample.pcm_bytes),exact(sample.audio_playback_frames),exact(sample.audio_failures),exact(sample.audio_discarded_bytes)])));
    html+='<p class="muted">— means unknown. Submission does not confirm visible output; audio acceptance does not confirm physical playback. A system-wide desktop trace is not an SNES frame acknowledgement.</p>';
    html+=panel('Lag and clock',U.table(['Batch','Maximum master lag','Maximum PCM queue','Remaining work','Clock','Generation','Resolution (ns)','Minimum clock pair (ns)','Regressions','Sampled operations'],run.samples.map(sample=>[exact(sample.sample),exact(sample.maximum_pending_master_cycles),exact(sample.maximum_pcm_queue_bytes),exact(sample.pending_master_cycles),E(sample.clock_source ?? '—'),exact(sample.clock_generation),exact(sample.clock_resolution_ns),exact(sample.clock_pair_min_ns),exact(sample.profile_clock_regressions),exact(sample.sampled_operations)])));
    const stages=['Guest step (including persistence)','Render PCM','Submit frame','Query host','Wait for host','Audio service'];
    html+=panel('Host phases · wall time (ns)',U.table(['Phase',...run.samples.map(sample=>'Batch '+sample.sample)],stages.map((label,index)=>[E(label),...run.samples.map(sample=>exact(sample.stage_wall_ns?.[index]))])));
    html+='<p class="muted">Persistence is included in the guest step and is not added again. The audio step also includes the final close/drain.</p>';
    html+=U.fold('Workload, inputs and state',run.workload)+U.fold('Environment, CPU, VM and conditions',run.environment)+U.fold('Host snapshot at import',run.host_snapshot)+U.fold('Artefacts, sources and versions',{artifacts:run.artifacts,versions:run.versions})+U.fold('Reported summary',run.summary);
    for(const sample of run.samples)html+=U.fold('Original normalised batch '+sample.sample+' including warm-up',sample);
    return html;
  }
  async function openRun(family,identifier){
    const generation=++B.detailGeneration;B.selection=null;
    U.open('Run',U.empty('Loading run'));
    try{
      const collection=family==='spectra'?'benchmark-runs':'subsystem-benchmark-runs';
      const run=await U.api('benchmarks/'+collection+'/'+encodeURIComponent(identifier)+(family==='spectra'?'?include_samples=false':''));
      if(generation!==B.detailGeneration || !document.querySelector('#detail-dialog')?.open)return;
      B.selection={family,run,sampleOffset:0,sampleTotal:run.sample_total};
      U.open((family==='spectra'?'Spectra':'SNES')+' · '+hostName(run.host_id),(B.detailActions?B.detailActions(family,run):'')+(family==='spectra'?spectraDetails(run):snesDetails(run))+(B.detailExtra?B.detailExtra(family,run):''));
      if(family==='spectra')await loadSamples();
    }catch(error){if(generation===B.detailGeneration && document.querySelector('#detail-dialog')?.open)U.open('Run unavailable','<p class="notice error">'+E(error.message)+'</p>');U.status(error.message,true);}
  }
  B.openRun=openRun;
  async function loadSamples(){
    const selected=B.selection;if(!selected || selected.family!=='spectra')return;
    const generation=++B.sampleGeneration,query=new URLSearchParams({limit:'50',offset:String(selected.sampleOffset)});
    const metric=document.querySelector('#sample-metric')?.value,phase=document.querySelector('#sample-phase')?.value;
    if(metric){const [area,id]=JSON.parse(metric);query.set('area_id',area);query.set('metric_id',id);}if(phase)query.set('phase',phase);
    const container=document.querySelector('#benchmark-samples');if(!container)return;container.innerHTML='<div class="loading-state">Loading raw samples …</div>';
    try{
      const page=await U.api('benchmarks/benchmark-runs/'+selected.run.run_id+'/samples?'+query);
      if(generation!==B.sampleGeneration || B.selection!==selected || !container.isConnected)return;
      selected.sampleTotal=page.total;
      container.innerHTML=(page.items.length?U.table(['Index / Phase','Area / metric','Status','Value','ns','Operations','Bytes','Clock / Generation','Resolution / observer (ns)','Workers completed / total'],page.items.map(sample=>[exact(sample.sample_index)+'<small class="cell-subline">'+E(sample.phase==='warmup'?'Warm-up':'Measurement')+'</small>',E(areas[sample.area_id])+'<small class="cell-subline">'+E(sample.metric_id)+'</small>',U.badge(sample.status),'<code>'+E(sample.value ?? '—')+'</code>',exact(sample.elapsed_ns),exact(sample.operation_count),exact(sample.bytes_processed),E(sample.clock_source ?? '—')+' / '+exact(sample.clock_generation),exact(sample.clock_resolution_ns)+' / '+exact(sample.observer_overhead_ns),exact(sample.completed_workers)+' / '+exact(sample.worker_count)])):U.empty('No matching raw samples'))+'<div class="pagination"><span>'+E(U.count(page.total))+' raw samples</span><div><button class="button subtle" data-sample-page="previous"'+(selected.sampleOffset===0?' disabled':'')+'>Previous</button><button class="button subtle" data-sample-page="next"'+(selected.sampleOffset+page.items.length>=page.total?' disabled':'')+'>Next</button></div></div>';
    }catch(error){if(container.isConnected)container.innerHTML='<p class="notice error">'+E(error.message)+'</p>';U.status(error.message,true);}
  }
  document.addEventListener('change',event=>{if(['sample-metric','sample-phase'].includes(event.target.id) && B.selection){B.selection.sampleOffset=0;loadSamples();}});
  document.addEventListener('click',event=>{const button=event.target.closest('[data-sample-page]');if(button && B.selection){B.selection.sampleOffset=Math.max(0,B.selection.sampleOffset+(button.dataset.samplePage==='next'?50:-50));loadSamples();}});
  document.addEventListener('close',event=>{if(event.target.id==='detail-dialog' && !event.target.open){B.detailGeneration++;B.sampleGeneration++;B.selection=null;}},true);
})();
