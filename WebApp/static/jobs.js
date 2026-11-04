"use strict";
(() => {
  const U=window.R4,E=U.esc;
  let value=null,inflight=null,again=false;
  const statuses={queued:'Queued',running:'Running',receiving:'Receiving',validating:'Validating'};
  function render(ctx,partial=true){
    if(ctx?.view!=='jobs'||ctx.generation!==U.generation)return;
    let html=!value?.complete?'<p class="workspace-notice" role="status">'+(value?'Some job sources are unavailable. The active count is incomplete.':'Job status is currently unavailable. Retrying automatically.')+'</p>':'';
    html+='<section id="active-jobs" class="record-section" aria-label="Active jobs">'+(value?.jobs.length?U.records(value.jobs.map(job=>{
      const progress=job.progress,percent=Number(progress?.percent),known=progress?.percent!==null&&progress?.percent!==undefined&&Number.isFinite(percent);
      const content='<h2 class="record-name">'+E(job.title)+'</h2><span class="record-subline">'+E(job.target)+'</span>'+U.fields([['Area',E(job.area)],['Status','<span class="badge">'+E(statuses[job.status]||job.status)+'</span>'],['Step',E(job.phase||'Waiting for the worker')],['Started',E(U.date(job.started||job.created))]])+(progress?'<progress class="job-progress" max="100"'+(known?' value="'+Math.min(100,Math.max(0,percent))+'"':'')+' aria-label="Job progress"></progress>':'');
      const actions=(job.log?'<button class="button subtle" data-repo-action="job" data-name="'+E(job.id)+'">Log</button>':'')+(job.details?'<button class="button subtle" data-update-id="'+E(job.id)+'">Details</button>':'')+(job.cancellable?'<button class="button subtle" data-job-cancel="'+E(job.id)+'">Cancel</button>':'');
      return U.record(content,actions,' data-record-key="'+E(job.key)+'"');
    }),'Active jobs'):U.empty(value?.complete?'No active jobs':value?'No available job data':'Loading jobs',value?'Queued and running work appears here across all areas.':''))+'</section>';
    U.page(ctx,'Jobs','',html,'',true,partial);
  }
  function badge(){
    const node=document.querySelector('#jobs-count');if(!node)return;
    const label=value?.complete?U.count(value.count):value?.known_count?U.count(value.known_count)+'+':'—';
    if(node.textContent!==label)node.textContent=label;
    node.toggleAttribute('data-incomplete',!value?.complete);
    node.title=value?.complete?value.count+' active jobs':'Job count is incomplete; a service is unavailable.';
  }
  function refresh(){
    if(inflight){again=true;return inflight;}
    inflight=U.api('jobs').then(data=>{value=data;badge();render(U.current);})
      .catch(()=>{value=value?{...value,complete:false}:null;badge();render(U.current);})
      .finally(()=>{inflight=null;if(again){again=false;refresh();}});
    return inflight;
  }
  U.jobs={refresh};
  U.views.jobs=async ctx=>{await refresh();render(ctx);};
  document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-job-cancel]');if(!button)return;
    button.disabled=true;
    try{await U.api('repositories/jobs/'+button.dataset.jobCancel+'/cancel',{method:'POST',body:{}});}
    catch(error){U.status(error.message,true);}
    finally{refresh();if(button.isConnected)button.disabled=false;}
  });
  document.addEventListener('DOMContentLoaded',()=>{refresh();setInterval(refresh,2500);});
})();
