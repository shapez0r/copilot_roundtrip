(() => {
  const vscode=acquireVsCodeApi();
  const $=id=>document.getElementById(id);
  const messages=$('messages'), form=$('form'), prompt=$('prompt'), repo=$('repo');
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
    for(const part of content) row.append(part);
    return row;
  }
  function span(cls,text){const s=document.createElement('span');s.className=cls;s.textContent=text;return s;}
  function section(title,label){
    const s=document.createElement('section');s.className='result-section';s.setAttribute('aria-label',label);
    const h=document.createElement('strong');h.textContent=title;s.append(h);
    const list=document.createElement('div');list.className='result-list';s.append(list);
    return {s,list};
  }
  function renderChanges(diffStat){
    const {s,list}=section('Changes','Changes');
    const lines=(diffStat||'').split(/\r?\n/).filter(l=>l.trim());
    let i=0, summary=null;
    for(const line of lines){
      const m=line.match(/^\s*(.+?)\s+\|\s+(\d+|Bin.*?)\s*([+-]*)\s*$/);
      if(!m){summary=line.trim();continue;}
      const graph=m[3]||'', plus=(graph.match(/\+/g)||[]).length, minus=(graph.match(/-/g)||[]).length;
      const kind=plus&&!minus?'added':minus&&!plus?'deleted':'modified';
      list.append(makeRow(kind,kind==='added'?'+':kind==='deleted'?'\u2212':'~',kind==='added'?'Insertions':kind==='deleted'?'Deletions':'Insertions and deletions',[span('result-path',m[1]),span('result-count',m[2])],i++));
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
  function renderGitStatus(gitStatus){
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
    if(m.type==='repos') {const previous=repo.value;repo.replaceChildren();for(const f of m.folders){const o=document.createElement('option');o.value=f.path;o.textContent=f.name+' — '+f.path;repo.append(o);}if([...repo.options].some(o=>o.value===previous))repo.value=previous;}
    if(m.type==='message')add(m.item);
    if(m.type==='model' && !$('model').dataset.initialized){$('model').value=m.value||'GPT/GPT-6';$('model').dataset.initialized='true';}
    if(m.type==='loginActive'){$('finishLogin').hidden=!m.value;$('login').disabled=m.value;activity.login=!!m.value;renderActivity();}
      if(m.type==='showBrowser' && !$('showBrowser').dataset.initialized){$('showBrowser').checked=!!m.value;$('showBrowser').dataset.initialized='true';}
      if(m.type==='history'){messages.replaceChildren();m.items.forEach(add);}
    if(m.type==='busy'){ activity.busy=!!m.value;renderActivity();$('send').disabled=m.value;$('cancel').disabled=!m.value;repo.disabled=m.value;prompt.disabled=m.value;$('model').disabled=m.value;$('showBrowser').disabled=m.value;}
    if(m.type==='progressHistory'){
      const last=m.items[m.items.length-1];
      activity.message=last?last.message:'';renderActivity();
    }
    if(m.type==='error'){activity.message=m.text;renderActivity();}
  });
  form.addEventListener('submit',e=>{e.preventDefault();if(!prompt.value.trim()||!repo.value)return;vscode.postMessage({type:'run',prompt:prompt.value,repo:repo.value,model:$('model').value,showBrowser:$('showBrowser').checked});prompt.value='';});
  $('cancel').addEventListener('click',()=>vscode.postMessage({type:'cancel'}));
  $('login').addEventListener('click',()=>vscode.postMessage({type:'login'}));
    $('finishLogin').addEventListener('click',()=>vscode.postMessage({type:'finishLogin'}));
  vscode.postMessage({type:'ready'});
})();
