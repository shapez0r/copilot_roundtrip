(() => {
  const vscode=acquireVsCodeApi();
  const $=id=>document.getElementById(id);
  const messages=$('messages'), form=$('form'), prompt=$('prompt');
  function add(item) {
    const card=document.createElement('article');card.className='message '+item.role;
    const heading=document.createElement('strong');heading.textContent=item.role==='user'?'You':item.role==='error'?'Error':'Copilot response';
    const text=document.createElement('pre');text.textContent=item.text||'';
    card.append(heading,text);
    messages.append(card);
    if(item.role==='assistant' && ('diffStat' in item || 'gitStatus' in item)) messages.append(renderChanges(item.diffStat),renderGitStatus(item.gitStatus));
    messages.scrollTop=messages.scrollHeight;
  }
  // Separate "Changes" and "Git status" sections, styled like Execution activity.
  function statusKind(code){
    if(code==='??'||code.includes('A')) return {kind:'added',badge:code==='??'?'U':'A',label:code==='??'?'Untracked':'Added'};
    if(code.includes('U')) return {kind:'deleted',badge:'!',label:'Conflict'};
    if(code.includes('D')) return {kind:'deleted',badge:'D',label:'Deleted'};
    if(code.includes('R')) return {kind:'modified',badge:'R',label:'Renamed'};
    if(code.includes('C')) return {kind:'added',badge:'C',label:'Copied'};
    return {kind:'modified',badge:'M',label:'Modified'};
  }
  function makeRow(kind,badge,label,content,index){
    const row=document.createElement('div');row.className='result-row '+kind;row.style.animationDelay=Math.min(index,20)*40+'ms';
    row.title=label;
    const icon=span('result-icon '+kind,badge);icon.setAttribute('aria-hidden','true');row.append(icon);
    for(const part of content) row.append(part);
    return row;
  }
  function span(cls,text){const s=document.createElement('span');s.className=cls;s.textContent=text;return s;}
  // Compact green/red squares: at most 5 per file however large the change (e.g. 1000+/100-).
  const BAR_BOXES=5;
  function diffBar(count,plus,minus){
    const total=parseInt(count,10), graph=plus+minus;
    if(!(total>0)||!graph) return null;
    const filled=Math.min(BAR_BOXES,total);
    let adds=Math.round(filled*plus/graph), dels=filled-adds;
    if(filled>1&&plus&&!adds){adds=1;dels--;}
    if(filled>1&&minus&&!dels){dels=1;adds--;}
    const bar=document.createElement('span');bar.className='result-bar';bar.setAttribute('aria-hidden','true');
    for(let k=0;k<BAR_BOXES;k++){const b=document.createElement('span');b.className=k<adds?'bar-add':k<adds+dels?'bar-del':'bar-none';bar.append(b);}
    return bar;
  }
  function section(title,label){
    const s=document.createElement('section');s.className='result-section';s.setAttribute('aria-label',label);
    const h=document.createElement('strong');h.textContent=title;s.append(h);
    const list=document.createElement('div');list.className='result-list';s.append(list);
    return {s,list};
  }
  function renderChanges(diffStat,repoPath){
    const {s,list}=section('Changes','Changes');
    const lines=(diffStat||'').split(/\r?\n/).filter(l=>l.trim());
    let i=0, summary=null;
    for(const line of lines){
      const m=line.match(/^\s*(.+?)\s+\|\s+(\d+|Bin.*?)\s*([+-]*)\s*$/);
      if(!m){summary=line.trim();continue;}
      const graph=m[3]||'', plus=(graph.match(/\+/g)||[]).length, minus=(graph.match(/-/g)||[]).length;
      const kind=plus&&!minus?'added':minus&&!plus?'deleted':'modified';
      list.append(makeRow(kind,kind==='added'?'+':kind==='deleted'?'\u2212':'~',kind==='added'?'Insertions':kind==='deleted'?'Deletions':'Insertions and deletions',[span('result-path',m[1]),span('result-count',m[2]),diffBar(m[2],plus,minus)].filter(Boolean),i++));
    }
    if(!i) list.append(makeRow('neutral','\u2713','No changes',[span('result-path','No tracked file changes.')],0));
    if(summary){
      const foot=document.createElement('div');foot.className='result-summary';
      const parts=summary.split(/,\s*/);
      parts.forEach((p,k)=>{const cls=/insertion/.test(p)?'sum-add':/deletion/.test(p)?'sum-del':'';foot.append(span(cls,p));if(k<parts.length-1)foot.append(document.createTextNode(', '));});
      s.append(foot);
    }
    return s;
  }
  function renderGitStatus(gitStatus,repoPath){
    const {s,list}=section('Git status','Git status');
    const lines=(gitStatus||'').split(/\r?\n/).filter(l=>l.trim());
    lines.forEach((line,i)=>{
      const code=line.slice(0,2), file=line.slice(3)||line.trim();
      const k=statusKind(code);
      list.append(makeRow(k.kind,k.badge,k.label,[span('result-path',file),span('result-code',code.trim()||code)],i));
    });
    if(!lines.length){const row=makeRow('added','\u2713','Clean',[span('result-path','Working tree clean.')],0);list.append(row);}
    return s;
  }
  // Single rotating activity line: "<latest status>" with a spinner while work is active.
  const activity={message:'',busy:false,login:false};
  function renderActivity(){
    const running=activity.busy||activity.login;
    const text=$('activityText'), spinner=$('activitySpinner');
    const next=running?(activity.message||'Starting...'):(activity.message||'Idle');
    spinner.hidden=!running;
    if(text.textContent!==next){text.textContent=next;text.classList.remove('rotate');void text.offsetWidth;text.classList.add('rotate');}
  }
  window.addEventListener('message',e=>{
    const m=e.data;
    if(m.type==='message')add(m.item);
    if(m.type==='model' && !$('model').dataset.initialized){$('model').value=m.value||'GPT/GPT-6';$('model').dataset.initialized='true';}
    if(m.type==='loginActive'){$('finishLogin').hidden=!m.value;$('login').disabled=m.value;activity.login=!!m.value;renderActivity();}
      if(m.type==='showBrowser' && !$('showBrowser').dataset.initialized){$('showBrowser').checked=!!m.value;$('showBrowser').dataset.initialized='true';}
      if(m.type==='history'){messages.replaceChildren();m.items.forEach(add);}
    if(m.type==='busy'){ activity.busy=!!m.value;renderActivity();$('send').disabled=m.value;$('cancel').disabled=!m.value;prompt.disabled=m.value;$('model').disabled=m.value;modelToggle.disabled=m.value;if(m.value)setModelOpen(false);$('showBrowser').disabled=m.value;}
    if(m.type==='progressHistory'){
      const last=m.items[m.items.length-1];
      activity.message=last?last.message:'';renderActivity();
    }
    if(m.type==='error'){activity.message=m.text;renderActivity();}
  });
    // Editable model combobox: pick a preset from the dropdown or type any provider/model.
    const modelInput=$('model'), modelList=$('modelList'), modelToggle=$('modelToggle'), modelCombo=$('modelCombo');
    const modelOptions=[...modelList.querySelectorAll('[role="option"]')];
    let activeOption=-1;
    function highlightModel(){
      modelOptions.forEach((o,k)=>{o.classList.toggle('active',k===activeOption);o.setAttribute('aria-selected',String(o.dataset.value===modelInput.value.trim()));});
      if(activeOption>=0) modelInput.setAttribute('aria-activedescendant',modelOptions[activeOption].id); else modelInput.removeAttribute('aria-activedescendant');
    }
    function setModelOpen(open){
      if(open&&modelInput.disabled) return;
      modelList.hidden=!open;modelInput.setAttribute('aria-expanded',String(open));modelToggle.setAttribute('aria-expanded',String(open));
      activeOption=open?modelOptions.findIndex(o=>o.dataset.value===modelInput.value.trim()):-1;
      highlightModel();
    }
    function chooseModel(o){modelInput.value=o.dataset.value;setModelOpen(false);modelInput.focus();}
    modelToggle.addEventListener('click',()=>{setModelOpen(modelList.hidden);modelInput.focus();});
    modelOptions.forEach(o=>{o.addEventListener('mousedown',e=>e.preventDefault());o.addEventListener('click',()=>chooseModel(o));});
    modelInput.addEventListener('keydown',e=>{
      const open=!modelList.hidden;
      if(e.key==='ArrowDown'||e.key==='ArrowUp'){
        e.preventDefault();
        if(!open){setModelOpen(true);return;}
        const n=modelOptions.length;activeOption=e.key==='ArrowDown'?(activeOption+1)%n:(activeOption<=0?n-1:activeOption-1);highlightModel();
      } else if(e.key==='Enter'&&open&&activeOption>=0){e.preventDefault();chooseModel(modelOptions[activeOption]);}
      else if(e.key==='Escape'&&open){e.preventDefault();setModelOpen(false);}
    });
    modelInput.addEventListener('input',()=>{if(!modelList.hidden)highlightModel();});
    modelCombo.addEventListener('focusout',e=>{if(!modelCombo.contains(e.relatedTarget))setModelOpen(false);});
    document.addEventListener('mousedown',e=>{if(!modelCombo.contains(e.target))setModelOpen(false);});
  form.addEventListener('submit',e=>{e.preventDefault();if(!prompt.value.trim())return;vscode.postMessage({type:'run',prompt:prompt.value,model:$('model').value,showBrowser:$('showBrowser').checked});prompt.value='';});
  $('cancel').addEventListener('click',()=>vscode.postMessage({type:'cancel'}));
  $('login').addEventListener('click',()=>vscode.postMessage({type:'login'}));
    $('finishLogin').addEventListener('click',()=>vscode.postMessage({type:'finishLogin'}));
  vscode.postMessage({type:'ready'});
})();
