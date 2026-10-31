(() => {
  const vscode=acquireVsCodeApi();
  const $=id=>document.getElementById(id);
  const messages=$('messages'), form=$('form'), prompt=$('prompt');
  function add(item, streamed=false) {
    const showText=item.role!=='assistant' || !streamed;
    const card=document.createElement('article');card.className='message '+item.role;
    const heading=document.createElement('strong');heading.textContent=item.role==='user'?'You':item.role==='error'?'Error':'';
    const text=document.createElement('pre');text.textContent=item.text||'';
    if(item.role==='assistant'){
      const head=document.createElement('div');head.className='message-head';
      const copy=document.createElement('button');copy.type='button';copy.className='message-copy';copy.textContent='Copy';
      copy.setAttribute('aria-label','Copy Copilot response');
      copy.addEventListener('click',()=>{copyText(item.text||'');copy.textContent='Copied';setTimeout(()=>{copy.textContent='Copy';},1500);});
      head.append(copy);card.append(head,text);
    } else if(item.role==='cancelled')card.append(text);
    else card.append(heading,text);
    if(Array.isArray(item.attachments)&&item.attachments.length){const att=document.createElement('div');att.className='message-attachments';att.textContent='Attached: '+item.attachments.join(', ');card.append(att);}
    if(showText) messages.append(card);
    if(item.role==='assistant' && ('diffStat' in item || 'gitStatus' in item)) messages.append(renderChanges(item.diffStat));
    $('conversation').scrollTop=$('conversation').scrollHeight;
  }
  // Webview clipboard access is unreliable in VS Code sidebars; the extension host writes the clipboard.
  function copyText(value){
    if(!value)return;
    vscode.postMessage({type:'copy',text:value});
  }
  // Handle keyboard and copy events through the extension host clipboard.
  function chatSelection(){
    const sel=window.getSelection();
    if(!sel||sel.isCollapsed||!sel.rangeCount)return '';
    const value=sel.toString();
    if(!value)return '';
    const r=sel.getRangeAt(0);
    const inside=n=>!!n&&messages.contains(n);
    return inside(sel.anchorNode)||inside(sel.focusNode)||inside(r.commonAncestorContainer)?value:'';
  }
  // Remember the last chat selection in case focus moves before the copy command runs.
  let lastSelection='', lastCopy={value:'',at:0};
  document.addEventListener('selectionchange',()=>{const v=chatSelection();if(v)lastSelection=v;});
  messages.addEventListener('mousedown',()=>{lastSelection='';});
  function copySelection(value){
    // Avoid a duplicate host write when both the keystroke and the copy event fire.
    const now=Date.now();
    if(value===lastCopy.value&&now-lastCopy.at<300)return;
    lastCopy={value,at:now};copyText(value);
  }
  function isEditable(t){return !!t&&(t.tagName==='TEXTAREA'||t.tagName==='INPUT'||t.isContentEditable);}
  window.addEventListener('keydown',e=>{
    if(e.altKey||e.shiftKey||!(e.ctrlKey||e.metaKey))return;
    const isC=String(e.key).toLowerCase()==='c'||e.code==='KeyC', isIns=e.ctrlKey&&e.key==='Insert';
    if(!isC&&!isIns)return;
    if(isEditable(e.target)||isEditable(document.activeElement))return;
    const value=chatSelection();
    if(!value)return;
    e.preventDefault();e.stopPropagation();copySelection(value);
  },true);
  document.addEventListener('copy',e=>{
    if(isEditable(document.activeElement))return;
    const value=chatSelection()||lastSelection;
    if(!value)return;
    if(e.clipboardData){e.clipboardData.setData('text/plain',value);e.preventDefault();}
    copySelection(value);
  });
  // Render the Changes summary with file counts and compact diff bars.
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
  // Show a compact status line until Copilot exposes structured activity.
  const activity={message:'',busy:false,login:false};
  let lastReasoning=[], lastAnswers=[], lastTasks=[];
  function renderActivity(){
    const running=activity.busy||activity.login;
    $('progress').hidden=!running&&!activity.message&&!lastReasoning.length&&!lastAnswers.length&&!lastTasks.length;
    $('activityLine').hidden=!!(lastTasks.length||lastReasoning.length)&&!activity.login;
    const text=$('activityText'), spinner=$('activitySpinner');
    const next=running?(activity.message||'Starting...'):(activity.message||'Idle');
    spinner.hidden=!running;
    if(text.textContent!==next){text.textContent=next;text.classList.remove('rotate');void text.offsetWidth;text.classList.add('rotate');}
  }
  // Mirror Copilot's task card and working timeline without exposing code blocks.
  let taskUserOpen=true, workUserOpen=false;
  const thoughtUserOpen={};
  const CODING_STEP=/^(coding\b|executing code\b|running code\b)/i;
  const THOUGHT_STEP=/^\s*(reasoning|reasoned|thinking|thought|thought for)\b/i;
  const CODING_PLACEHOLDER='Code and command output are hidden.';
  const liveItems=new Map();
  function sectionSteps(b){
    const steps=(Array.isArray(b.steps)?b.steps:[]).filter(s=>s&&typeof s==='object')
      .map(s=>{
        const title=String(s.title||''), lines=Array.isArray(s.lines)?s.lines:[], total=Math.max(Number(s.total)||0,lines.length);
        if(s.kind==='coding'||CODING_STEP.test(title))return {title,kind:'coding',lines:[],total:0,coding:true};
        if(s.kind==='completed'||(!THOUGHT_STEP.test(title)&&total===0))return {title,kind:'completed',lines:[],total:0};
        return {...s,title,kind:'thought',lines,total};
      });
    if(steps.length)return steps;
    const lines=Array.isArray(b.lines)?b.lines:[];
    return lines.length?[{title:'Reasoning',lines,total:b.total||lines.length}]:[];
  }
  function taskItem(){
    const key='task';
    if(liveItems.has(key))return liveItems.get(key);
    const d=document.createElement('section');d.className='task-card';
    const head=document.createElement('div');head.className='task-head';
    const toggle=document.createElement('button');toggle.type='button';toggle.className='task-toggle';
    const titleBox=document.createElement('span');titleBox.className='task-heading';
    const title=span('task-title',''), status=span('task-status','');titleBox.append(title,status);
    const chev=span('task-chevron','\u2303');chev.setAttribute('aria-hidden','true');
    toggle.append(span('task-state',''),titleBox,chev);
    const stop=document.createElement('button');stop.type='button';stop.className='task-stop';stop.title='Stop task';stop.setAttribute('aria-label','Stop task');
    stop.append(span('task-stop-icon',''));head.append(toggle,stop);
    const plan=document.createElement('div');plan.className='task-plan';
    d.append(head,plan);
    toggle.addEventListener('click',()=>{if(toggle.disabled)return;taskUserOpen=!taskUserOpen;renderReasoning(lastReasoning,lastAnswers);});
    stop.addEventListener('click',()=>{stop.disabled=true;vscode.postMessage({type:'cancel'});});
    liveItems.set(key,d);return d;
  }
  function taskState(value){return /^(completed|active|pending|failed)$/.test(String(value||'').toLowerCase())?String(value).toLowerCase():'pending';}
  function updateTask(task){
    const d=taskItem(), head=d.firstChild, toggle=head.firstChild, stop=head.lastChild, plan=d.lastChild;
    const stopped=/stop|cancel/i.test(task.state||'');
    const state=stopped?'stopped':task.active?'active':/fail/i.test(task.state||'')?'failed':/complete|done/i.test(task.state||'')?'completed':'pending';
    const marker=toggle.children[0];marker.className='task-state '+state;marker.textContent=state==='completed'?'\u2713':state==='stopped'?'\u00d7':'';
    toggle.children[1].children[0].textContent=task.title||'Copilot task';
    toggle.children[1].children[1].textContent=stopped?'Task stopped':(task.state||'In progress');
    const expandable=!stopped&&Array.isArray(task.steps)&&task.steps.length>0;
    toggle.disabled=!expandable;toggle.setAttribute('aria-expanded',String(expandable&&taskUserOpen));
    toggle.title=expandable?(taskUserOpen?'Collapse task plan':'Expand task plan'):'';
    toggle.children[2].hidden=!expandable;
    const canStop=activity.busy&&task.active&&!stopped;
    stop.hidden=!canStop;stop.disabled=!canStop;
    d.classList.toggle('running',canStop);d.classList.toggle('stopped',stopped);
    plan.hidden=!expandable||!taskUserOpen;plan.replaceChildren();
    for(const item of Array.isArray(task.steps)?task.steps:[]){
      const row=document.createElement('div');row.className='task-row';
      const itemState=taskState(item.state), icon=span('task-row-state '+itemState,itemState==='completed'?'\u2713':'');
      row.dataset.state=itemState;row.setAttribute('aria-label',(item.title||'Plan item')+': '+itemState);
      icon.setAttribute('aria-hidden','true');row.append(icon,span('task-row-title',item.title||''));plan.append(row);
    }
    return d;
  }
  function setText(el,value){if(el.textContent!==value){el.textContent=value;el.scrollTop=el.scrollHeight;}}
  function answerItem(after){
    const key='a'+after;
    if(liveItems.has(key))return liveItems.get(key);
    const d=document.createElement('div');d.className='live-answer';
    const pre=document.createElement('pre');pre.className='live-answer-text';
    d.append(pre);
    liveItems.set(key,d);return d;
  }
  function workItem(){
    const key='work';
    if(liveItems.has(key))return liveItems.get(key);
    const d=document.createElement('section');d.className='work-section';
    const head=document.createElement('button');head.type='button';head.className='work-head';
    const icon=span('work-state','');icon.setAttribute('aria-hidden','true');
    const chev=span('work-chevron','\u2304');chev.setAttribute('aria-hidden','true');
    const title=span('work-title','');
    title.append(span('work-title-prefix','Working through the steps: '),span('work-title-step',''));
    title.setAttribute('role','status');title.setAttribute('aria-live','polite');
    head.append(icon,title,span('work-toggle-label','Show steps'),chev);
    const timeline=document.createElement('div');timeline.className='work-timeline';timeline.id='workingSteps';
    head.setAttribute('aria-controls',timeline.id);
    d.append(head,timeline);
    head.addEventListener('click',()=>{workUserOpen=!workUserOpen;renderReasoning(lastReasoning,lastAnswers);});
    liveItems.set(key,d);return d;
  }
  function updateAnswer(after,text,lastAfter,insideWork=false){
    const el=answerItem(after), pre=el.firstChild;
    el.classList.toggle('active',activity.busy&&after===lastAfter);
    el.classList.toggle('interim',insideWork);
    el.classList.toggle('work-activity',insideWork);
    if(pre.textContent!==text){pre.textContent=text;pre.scrollTop=pre.scrollHeight;}
    return el;
  }
  function thoughtItem(blockIndex,stepIndex,step){
    const key=blockIndex+':'+stepIndex, coding=!!step.coding;
    const item=document.createElement('div');item.className='thought'+(coding?' coding':'');
    const toggle=document.createElement('button');toggle.type='button';toggle.className='thought-head';
    const chev=span('thought-chevron','\u2304');chev.setAttribute('aria-hidden','true');
    toggle.append(span('thought-dot',''),span('thought-title',coding?(step.title||'Coding and executing'):'Thought process'),chev);
    const prose=document.createElement('div');prose.className='thought-prose';
    const open=key in thoughtUserOpen?thoughtUserOpen[key]:false;
    toggle.setAttribute('aria-expanded',String(open));toggle.title=open?'Collapse details':'Expand details';
    prose.hidden=!open;setText(prose,open?(coding?CODING_PLACEHOLDER:((step.lines||[]).join('\n')||'No details available.')):'');
    toggle.addEventListener('click',()=>{thoughtUserOpen[key]=!open;renderReasoning(lastReasoning,lastAnswers);});
    item.append(toggle,prose);return item;
  }
  function completedStepItem(step){
    const item=document.createElement('div');item.className='work-completed';
    item.setAttribute('aria-label',(step.title||'Completed step')+': completed');
    const marker=span('work-completed-state','\u2713');marker.setAttribute('aria-hidden','true');
    item.append(marker,span('work-completed-title',step.title||'Completed step'));return item;
  }
  // Keep the latest completed title until another step completes; use the active title initially.
  function workSummary(blocks){
    let completed='', current='';
    for(const block of blocks){
      const details=sectionSteps(block);
      const finished=details.filter(detail=>detail.kind==='completed');
      if(finished.length)completed=finished[finished.length-1].title||completed;
      else if(!block.active)completed=(details.length?details[details.length-1].title:block.title)||completed;
      if(block.active)current=(details.length?details[details.length-1].title:block.title)||current;
    }
    return completed||current||'Reasoning';
  }
  function updateWork(blocks,byAfter,lastAfter){
    const d=workItem(), head=d.firstChild, timeline=d.lastChild;
    const running=activity.busy&&blocks.some(block=>block.active), marker=head.children[0], title=head.children[1];
    marker.className='work-state'+(running?' spinner':'');marker.textContent=running?'':'\u2713';
    const next='Working through the steps: '+workSummary(blocks);
    if(title.textContent!==next){
      title.children[1].textContent=workSummary(blocks);title.classList.remove('rotate');void title.offsetWidth;title.classList.add('rotate');
    }
    title.title=next;
    head.children[2].textContent=workUserOpen?'Hide steps':'Show steps';
    head.setAttribute('aria-expanded',String(workUserOpen));head.title=workUserOpen?'Hide working steps':'Show working steps';
    timeline.hidden=!workUserOpen;timeline.replaceChildren();
    blocks.forEach((block,i)=>{
      const active=activity.busy&&block.active;
      const step=document.createElement('div');step.className='work-step'+(active?' active':' completed');
      const top=document.createElement('div');top.className='work-step-top';
      const marker=span('work-step-state'+(active?' spinner':''),active?'':'\u2713');marker.setAttribute('aria-hidden','true');
      top.append(marker,span('work-step-title',block.title||'Reasoning'));step.append(top);
      sectionSteps(block).forEach((detail,k)=>step.append(detail.kind==='completed'?completedStepItem(detail):thoughtItem(i,k,detail)));
      if(byAfter.has(i+1))step.append(updateAnswer(i+1,byAfter.get(i+1),lastAfter,true));
      timeline.append(step);
    });
    return d;
  }
  function stoppedItem(task){
    const key='stopped';
    if(!liveItems.has(key)){
      const d=document.createElement('div');d.className='task-stopped-response';
      const pre=document.createElement('pre');pre.className='task-stopped-text';d.append(pre);liveItems.set(key,d);
    }
    const d=liveItems.get(key);
    setText(d.firstChild,task.response||'The task was cancelled before it could finish.');
    return d;
  }
  function renderReasoning(blocks,answers){
    const box=$('reasoning'), list=$('reasoningBlocks');
    blocks=Array.isArray(blocks)?blocks:[];
    answers=(Array.isArray(answers)?answers:[]).filter(a=>a&&typeof a.text==='string'&&a.text.trim());
    const task=lastTasks.length?lastTasks[lastTasks.length-1]:null, stopped=!!task&&/stop|cancel/i.test(task.state||'');
    if(stopped){blocks=[];answers=answers.filter(a=>(Number(a.after)||0)===0);}
    if(!blocks.length&&!answers.length&&!task){workUserOpen=false;box.hidden=true;list.replaceChildren();liveItems.clear();for(const k of Object.keys(thoughtUserOpen))delete thoughtUserOpen[k];return;}
    box.hidden=false;
    const byAfter=new Map();
    for(const a of answers){const k=Math.max(0,Math.min(blocks.length,Number(a.after)||0));byAfter.set(k,(byAfter.has(k)?byAfter.get(k)+'\n\n':'')+a.text.trim());}
    const ordered=[], lastAfter=Math.max(-1,...byAfter.keys());
    const pushAnswer=k=>{
      if(!byAfter.has(k))return;
      ordered.push(updateAnswer(k,byAfter.get(k),lastAfter,false));
    };
    pushAnswer(0);
    if(task)ordered.push(updateTask(task));
    if(stopped)ordered.push(stoppedItem(task));
    else if(blocks.length)ordered.push(updateWork(blocks,byAfter,lastAfter));
    // Drop items that disappeared, then place the rest in order without recreating them.
    const keep=new Set(ordered);
    for(const [k,el] of liveItems) if(!keep.has(el)&&!ordered.some(parent=>parent.contains(el))){el.remove();liveItems.delete(k);}
    ordered.forEach((el,k)=>{if(list.children[k]!==el)list.insertBefore(el,list.children[k]||null);});
    while(list.children.length>ordered.length)list.lastChild.remove();
  }
  window.addEventListener('message',e=>{
    const m=e.data;
    if(m.type==='message'){setHistoryOpen(false);add(m.item,lastAnswers.some(a=>typeof a.text==='string'&&a.text.trim()));}
    if(m.type==='model' && !$('model').dataset.initialized){$('model').value=m.value||'GPT/GPT-6';$('model').dataset.initialized='true';}
    if(m.type==='loginActive'){if(m.value){$('settingsPanel').hidden=false;$('settingsToggle').setAttribute('aria-expanded','true');}$('finishLogin').hidden=!m.value;$('login').disabled=m.value;activity.login=!!m.value;renderActivity();}
      if(m.type==='showBrowser' && !$('showBrowser').dataset.initialized){$('showBrowser').checked=!!m.value;$('showBrowser').dataset.initialized='true';}
      if(m.type==='temporaryChat' && !$('temporaryChat').dataset.initialized){tempPref=m.value!==false;$('temporaryChat').dataset.initialized='true';syncTemporary();}
      if(m.type==='chats'){chats=Array.isArray(m.items)?m.items:[];renderHistory();}
      if(m.type==='chatState'){chatState=m;renderChatState();renderHistory();}
      if(m.type==='toggleHistory')setHistoryOpen($('historyPanel').hidden);
      if(m.type==='focusPrompt'&&!prompt.disabled)prompt.focus();
      if(m.type==='history'){messages.replaceChildren();m.items.forEach(item=>add(item));setHistoryOpen(!m.items.length);}
    if(m.type==='busy'){ activity.busy=!!m.value;renderActivity();renderReasoning(lastReasoning,lastAnswers);$('send').disabled=m.value;$('cancel').disabled=!m.value;$('cancel').hidden=!m.value;$('send').hidden=!!m.value;prompt.disabled=m.value;$('model').disabled=m.value;modelToggle.disabled=m.value;if(m.value)setModelOpen(false);$('showBrowser').disabled=m.value;syncTemporary();$('newChat').disabled=m.value;renderHistory();renderAttachments();}
    if(m.type==='progressHistory'){
      const last=m.items[m.items.length-1];
      activity.message=last?last.message:'';renderActivity();
    }
    if(m.type==='error'){activity.message=m.text;renderActivity();}
    if(m.type==='tasks'){lastTasks=Array.isArray(m.items)?m.items:[];renderActivity();renderReasoning(lastReasoning,lastAnswers);}
    if(m.type==='reasoning'){lastReasoning=m.blocks||[];lastAnswers=m.answers||[];renderActivity();renderReasoning(lastReasoning,lastAnswers);}
    if(m.type==='attached'&&Array.isArray(m.items)){for(const it of m.items)addAttachment(it);}
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
  // Additional prompt attachments: paste an image, drag and drop images/files, or pick files.
  const MAX_BYTES=50*1024*1024, IMAGE_EXT={'image/png':'png','image/jpeg':'jpg','image/gif':'gif','image/webp':'webp','image/bmp':'bmp'};
  const attachList=$('attachments');
  let attachments=[], attachSeq=0;
  function formatSize(n){return n>=1048576?(n/1048576).toFixed(1)+' MB':n>=1024?Math.round(n/1024)+' KB':n+' B';}
  function addAttachment(it){
    if(it.kind==='path'&&attachments.some(a=>a.kind==='path'&&a.path===it.path))return;
    attachments.push({...it,id:++attachSeq});renderAttachments();
  }
  function renderAttachments(){
    attachList.replaceChildren();attachList.hidden=!attachments.length;
    const busy=$('send').disabled;
    $('attach').disabled=busy;
    for(const a of attachments){
      const li=document.createElement('li');li.className='attachment';li.title=a.path||a.name;
      if(a.preview){const img=document.createElement('img');img.src=a.preview;img.alt='';li.append(img);}
      else li.append(span('attachment-icon','\u{1F4CE}'));
      li.append(span('attachment-name',a.name));
      if(typeof a.size==='number')li.append(span('attachment-size',formatSize(a.size)));
      const rm=document.createElement('button');rm.type='button';rm.className='attachment-remove';rm.textContent='\u00d7';rm.disabled=busy;
      rm.setAttribute('aria-label','Remove '+a.name);
      rm.addEventListener('click',()=>{attachments=attachments.filter(x=>x.id!==a.id);renderAttachments();});
      li.append(rm);attachList.append(li);
    }
  }
  function readFile(file,name){
    if(file.size>MAX_BYTES){activity.message='Attachment is larger than 50 MB: '+name;renderActivity();return;}
    const reader=new FileReader();
    reader.onload=()=>{
      const url=String(reader.result||''), comma=url.indexOf(',');
      if(comma<0)return;
      addAttachment({kind:'data',name,size:file.size,data:url.slice(comma+1),preview:/^image\//.test(file.type)?url:''});
    };
    reader.onerror=()=>{activity.message='Could not read attachment: '+name;renderActivity();};
    reader.readAsDataURL(file);
  }
  function stamp(){const d=new Date(),p=n=>String(n).padStart(2,'0');return d.getFullYear()+p(d.getMonth()+1)+p(d.getDate())+'-'+p(d.getHours())+p(d.getMinutes())+p(d.getSeconds());}
  function addFiles(files,pasted){
    let k=0;
    for(const f of files){
      if(!f)continue;
      // Clipboard images usually arrive as a generic "image.png"; give them unique names.
      const name=pasted&&/^image\//.test(f.type)?'pasted-image-'+stamp()+'-'+(++k)+'.'+(IMAGE_EXT[f.type]||'png'):(f.name||'attachment');
      readFile(f,name);
    }
  }
  // Clipboard files may appear in items and/or files depending on the source; collect both, deduplicated.
  function clipboardFiles(cd){
    const out=[];
    for(const i of [...(cd.items||[])]) if(i.kind==='file'){const f=i.getAsFile();if(f)out.push(f);}
    for(const f of [...(cd.files||[])]) if(!out.some(o=>o.name===f.name&&o.size===f.size&&o.type===f.type))out.push(f);
    return out;
  }
  // Listen on the document so pastes are recognized wherever focus is in the sidebar.
  document.addEventListener('paste',e=>{
    if(prompt.disabled||!e.clipboardData)return;
    const files=clipboardFiles(e.clipboardData);
    if(!files.length)return; // ordinary text paste
    e.preventDefault();addFiles(files,true);
  });
  let dragDepth=0;
  form.addEventListener('dragenter',e=>{e.preventDefault();if(++dragDepth===1&&!prompt.disabled)form.classList.add('dragging');});
  form.addEventListener('dragleave',()=>{if(--dragDepth<=0){dragDepth=0;form.classList.remove('dragging');}});
  form.addEventListener('dragover',e=>{e.preventDefault();if(e.dataTransfer)e.dataTransfer.dropEffect=prompt.disabled?'none':'copy';});
  form.addEventListener('drop',e=>{
    e.preventDefault();dragDepth=0;form.classList.remove('dragging');
    if(prompt.disabled||!e.dataTransfer)return;
    const files=[...(e.dataTransfer.files||[])];
    if(files.length){addFiles(files,false);return;}
    // Drops from the VS Code Explorer provide URIs instead of file contents.
    const dt=e.dataTransfer, get=t=>{try{return dt.getData(t)||'';}catch(_){return '';}};
    const lines=t=>get(t).split(/\r?\n/).map(u=>u.trim()).filter(u=>u&&!u.startsWith('#'));
    const json=t=>{try{const v=JSON.parse(get(t)||'[]');return Array.isArray(v)?v.map(String):[];}catch(_){return [];}};
    let list=lines('text/uri-list');
    for(const t of ['application/vnd.code.uri-list','resourceurls','codefiles']) if(!list.length) list=json(t);
    if(!list.length) list=lines('text/plain').filter(u=>/^(file:|[a-zA-Z]:[\\/]|\/|\\\\)/.test(u));
    if(list.length)vscode.postMessage({type:'attachUris',uris:list});
    else{activity.message='Dropped item was not recognized as a file. Hold Shift while dropping, or use Attach files...';renderActivity();}
  });
  document.addEventListener('dragover',e=>e.preventDefault());
  document.addEventListener('drop',e=>{if(!form.contains(e.target))e.preventDefault();});
  $('attach').addEventListener('click',()=>vscode.postMessage({type:'pickAttachments'}));
  form.addEventListener('submit',e=>{e.preventDefault();if(!prompt.value.trim())return;vscode.postMessage({type:'run',prompt:prompt.value,model:$('model').value,showBrowser:$('showBrowser').checked,temporaryChat:$('temporaryChat').checked,attachments:attachments.map(a=>a.kind==='path'?{kind:'path',path:a.path,name:a.name}:{kind:'data',name:a.name,data:a.data})});prompt.value='';resizePrompt();attachments=[];renderAttachments();});
  // Enter runs the prompt exactly like clicking Modify repository; Shift+Enter inserts a new line.
  prompt.addEventListener('keydown',e=>{
    if(e.key!=='Enter'||e.shiftKey||e.altKey||e.isComposing||e.keyCode===229)return;
    e.preventDefault();
    if($('send').disabled)return;
    form.requestSubmit($('send'));
  });
  $('cancel').addEventListener('click',()=>vscode.postMessage({type:'cancel'}));
  // Lock saved or temporary mode after the first prompt until New chat.
  let chats=[], chatState={id:null,temporary:null,started:false,linked:false,title:''}, tempPref=true;
  $('temporaryChat').addEventListener('change',()=>{if(!chatLocked())tempPref=$('temporaryChat').checked;});
  function chatLocked(){return !!chatState.id||!!chatState.started;}
  function syncTemporary(){
    const box=$('temporaryChat'), locked=chatLocked();
    box.checked=chatState.id?false:locked&&chatState.temporary!==null?!!chatState.temporary:tempPref;
    box.disabled=activity.busy||locked;
    box.parentElement.title=locked?'Fixed for this chat. Start a New chat to change it.':'Enable Copilot Temporary chat before selecting the model (temporary chats are not saved in History)';
  }
  function renderChatState(){
    const t=$('chatTitle');
    t.textContent=chatState.id?(chatState.title||'Saved chat'):chatState.started&&chatState.temporary?'Temporary chat (not saved)':'New chat';
    t.title=chatState.id?(chatState.linked?'Saved chat: prompts continue the same Copilot chat':'Saved chat: the Copilot chat link is recorded after the first prompt'):t.textContent;
    syncTemporary();
  }
  function formatWhen(ms){
    if(!ms)return '';
    const d=new Date(ms), now=new Date();
    return d.toDateString()===now.toDateString()?d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}):d.toLocaleDateString();
  }
  function setHistoryOpen(open){
    $('historyPanel').hidden=!open;$('historyToggle').setAttribute('aria-expanded',String(open));
    $('historyToggle').title=open?'Hide chat history':'Show chat history';
      $('historyToggle').setAttribute('aria-label',$('historyToggle').title);
    if(open)renderHistory();
  }
  function renderHistory(){
    const list=$('historyList');
    list.replaceChildren();
    $('historyEmpty').hidden=chats.length>0;
    for(const c of chats){
      const li=document.createElement('li');li.className='history-item'+(c.id===chatState.id?' active':'');
      const open=document.createElement('button');open.type='button';open.className='history-open';open.disabled=activity.busy;
      if(c.id===chatState.id)open.setAttribute('aria-current','true');
      const meta=[formatWhen(c.updated),c.prompts+(c.prompts===1?' prompt':' prompts'),c.model].filter(Boolean).join(' \u00b7 ');
      open.append(span('history-title',c.title),span('history-meta',meta));
      open.title=c.linked?'Open this chat; new prompts continue the same Copilot chat':'Open this chat';
      open.addEventListener('click',()=>{vscode.postMessage({type:'openChat',id:c.id});setHistoryOpen(false);});
      const del=document.createElement('button');del.type='button';del.className='history-delete';del.textContent='\u00d7';del.disabled=activity.busy;
      del.setAttribute('aria-label','Delete chat '+c.title);del.title='Delete from history';
      del.addEventListener('click',()=>vscode.postMessage({type:'deleteChat',id:c.id}));
      li.append(open,del);list.append(li);
    }
  }
  $('newChat').addEventListener('click',()=>{vscode.postMessage({type:'newChat'});setHistoryOpen(false);});
  $('historyToggle').addEventListener('click',()=>setHistoryOpen($('historyPanel').hidden));
  $('login').addEventListener('click',()=>vscode.postMessage({type:'login'}));
    $('finishLogin').addEventListener('click',()=>vscode.postMessage({type:'finishLogin'}));
  // Keep the composer compact until a multiline request needs more space.
    function resizePrompt(){prompt.style.height='auto';prompt.style.height=Math.min(180,Math.max(66,prompt.scrollHeight))+'px';}
    prompt.addEventListener('input',resizePrompt);
    $('settingsToggle').addEventListener('click',()=>{const panel=$('settingsPanel');panel.hidden=!panel.hidden;$('settingsToggle').setAttribute('aria-expanded',String(!panel.hidden));});
    document.addEventListener('mousedown',e=>{if(!$('settingsPanel').hidden&&!$('settingsPanel').contains(e.target)&&!$('settingsToggle').contains(e.target)){$('settingsPanel').hidden=true;$('settingsToggle').setAttribute('aria-expanded','false');}});
    document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('settingsPanel').hidden){$('settingsPanel').hidden=true;$('settingsToggle').setAttribute('aria-expanded','false');$('settingsToggle').focus();}});
    vscode.postMessage({type:'ready'});
})();
