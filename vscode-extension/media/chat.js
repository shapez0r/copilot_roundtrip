(() => {
  const vscode=acquireVsCodeApi();
  const $=id=>document.getElementById(id);
  const messages=$('messages'), form=$('form'), prompt=$('prompt'), repo=$('repo');
  function add(item) {
    const card=document.createElement('article');card.className='message '+item.role;
    const heading=document.createElement('strong');heading.textContent=item.role==='user'?'You':item.role==='error'?'Error':'Copilot response';
    const text=document.createElement('pre');text.textContent=item.text||'';
    card.append(heading,text);
    if(item.result){const b=document.createElement('button');b.type='button';b.textContent='Open modified ZIP';b.addEventListener('click',()=>vscode.postMessage({type:'openResult',path:item.result}));card.append(b);}
    messages.append(card);messages.scrollTop=messages.scrollHeight;
  }
  window.addEventListener('message',e=>{
    const m=e.data;
    if(m.type==='repos') {const previous=repo.value;repo.replaceChildren();for(const f of m.folders){const o=document.createElement('option');o.value=f.path;o.textContent=f.name+' — '+f.path;repo.append(o);}if([...repo.options].some(o=>o.value===previous))repo.value=previous;}
    if(m.type==='message')add(m.item);
    if(m.type==='history'){messages.replaceChildren();m.items.forEach(add);}
    if(m.type==='busy'){ $('send').disabled=m.value;$('cancel').disabled=!m.value;repo.disabled=m.value;prompt.disabled=m.value;}
    if(m.type==='status')$('status').textContent=m.text;
    if(m.type==='error')$('status').textContent=m.text;
  });
  form.addEventListener('submit',e=>{e.preventDefault();if(!prompt.value.trim()||!repo.value)return;$('status').textContent='Starting...';vscode.postMessage({type:'run',prompt:prompt.value,repo:repo.value});prompt.value='';});
  $('cancel').addEventListener('click',()=>vscode.postMessage({type:'cancel'}));
  $('login').addEventListener('click',()=>vscode.postMessage({type:'login'}));
  vscode.postMessage({type:'ready'});
})();
