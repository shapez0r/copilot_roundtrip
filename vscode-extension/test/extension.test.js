const test=require('node:test');const assert=require('node:assert/strict');const Module=require('node:module');const original=Module._load;Module._load=function(id,...rest){if(id==='vscode')return {};return original.call(this,id,...rest)};const {buildArgs,parseResult}=require('../extension');Module._load=original;test('argument boundaries preserve prompt',()=>{const a=buildArgs('/script.py','change "this"; echo unsafe','/repo',{model:'auto',timeout:900});assert.equal(a[2],'change "this"; echo unsafe');assert.equal(a[4],'/repo')});test('JSON result parsed',()=>assert.equal(parseResult('{"result":"/tmp/a.zip"}').result,'/tmp/a.zip'));test('invalid JSON rejected',()=>assert.throws(()=>parseResult('broken')));

const {parseStatusLine}=require('../extension');
test('structured status is parsed',()=>assert.deepEqual(parseStatusLine('ROUNDTRIP_STATUS:{"stage":"uploading","message":"Uploading..."}'),{stage:'uploading',message:'Uploading...'}));
test('task status preserves the structured plan',()=>assert.deepEqual(parseStatusLine('ROUNDTRIP_STATUS:'+JSON.stringify({stage:'task',message:'Working',tasks:[{title:'Create games',state:'In progress',active:true,steps:[{title:'Inspect files',state:'completed'},{title:'Build UI',state:'active'},{title:'Test',state:'pending'}]}]})),{stage:'task',message:'Working',tasks:[{title:'Create games',state:'In progress',active:true,steps:[{title:'Inspect files',state:'completed'},{title:'Build UI',state:'active'},{title:'Test',state:'pending'}]}]}));
test('stopped task status preserves its cancellation response',()=>assert.deepEqual(parseStatusLine('ROUNDTRIP_STATUS:'+JSON.stringify({stage:'cancelled',message:'Stopped',tasks:[{title:'Create games',state:'Task stopped',active:false,steps:[],response:'The task was cancelled before it could finish.'}]})),{stage:'cancelled',message:'Stopped',tasks:[{title:'Create games',state:'Task stopped',active:false,steps:[],response:'The task was cancelled before it could finish.'}]}));
test('non-status and malformed lines are ignored',()=>{assert.equal(parseStatusLine('warning'),null);assert.equal(parseStatusLine('ROUNDTRIP_STATUS:{'),null)});

test('model selection preserves provider/model slash',()=>{const a=buildArgs('/script.py','fix','/repo',{model:'Claude/Opus 5.5',timeout:900});assert.equal(a[a.indexOf('--model')+1],'Claude/Opus 5.5')});

const {resolveChangedPath}=require('../extension');
test('changed file paths resolve for diff links',()=>{const st=' M src/app.js\nR  old.txt -> new.txt\n?? "a b.txt"\n';const ds=' src/app.js | 2 +-\n .../deep/nested/file.js | 1 +\n';assert.equal(resolveChangedPath('src/app.js',st,ds),'src/app.js');assert.equal(resolveChangedPath('old.txt -> new.txt',st,ds),'new.txt');assert.equal(resolveChangedPath('"a b.txt"',st,ds),'a b.txt');assert.equal(resolveChangedPath('../etc/passwd',st,ds),null)});
const {materializeAttachments,safeAttachmentName}=require('../extension');const fsT=require('node:fs'),osT=require('node:os'),pathT=require('node:path');
test('attachments become repeated --attach arguments',()=>{const a=buildArgs('/script.py','fix','/repo',{model:'GPT/GPT-6',timeout:900,attachments:['/a/pic.png','/b/spec.pdf']});assert.deepEqual(a.slice(-4),['--attach','/a/pic.png','--attach','/b/spec.pdf']);assert.equal(a[2],'fix')});
test('pasted image bytes and file paths are materialized',()=>{const base=fsT.mkdtempSync(pathT.join(osT.tmpdir(),'rt-test-'));try{const existing=pathT.join(base,'notes.txt');fsT.writeFileSync(existing,'x');const r=materializeAttachments([{kind:'data',name:'../evil/pasted.png',data:Buffer.from('png').toString('base64')},{kind:'data',name:'pasted.png',data:Buffer.from('png2').toString('base64')},{kind:'path',path:existing}],()=>{const d=pathT.join(base,'out');fsT.mkdirSync(d);return d});assert.equal(r.files.length,3);assert.equal(pathT.basename(r.files[0]),'pasted.png');assert.equal(pathT.basename(r.files[1]),'2-pasted.png');assert.equal(pathT.dirname(r.files[0]),r.dir);assert.equal(fsT.readFileSync(r.files[1],'utf8'),'png2');assert.equal(r.files[2],existing);assert.throws(()=>materializeAttachments([{kind:'path',path:pathT.join(base,'missing')}],()=>base))}finally{fsT.rmSync(base,{recursive:true,force:true})}});
test('attachment names are sanitized',()=>{assert.equal(safeAttachmentName('a:b?.png','x'),'a_b_.png');assert.equal(safeAttachmentName('..','fallback'),'fallback')});
test('temporary chat is enabled by default and can be turned off',()=>{const on=buildArgs('/script.py','fix','/repo',{model:'GPT/GPT-6',timeout:900});assert.ok(!on.includes('--no-temporary-chat'));const off=buildArgs('/script.py','fix','/repo',{model:'GPT/GPT-6',timeout:900,temporaryChat:false});assert.ok(off.includes('--no-temporary-chat'))});

const {parseReasoningLine}=require('../extension');
test('reasoning lines are parsed and limited to the latest 10 lines per block',()=>{const lines=Array.from({length:15},(_,i)=>'step '+i);const r=parseReasoningLine('ROUNDTRIP_REASONING:'+JSON.stringify({blocks:[{title:'Reasoned for 38s',active:false,total:15,lines},{title:'Reasoning',active:true,lines:['a']}]}));assert.equal(r.length,2);assert.equal(r[0].lines.length,10);assert.equal(r[0].lines[0],'step 5');assert.equal(r[0].total,15);assert.equal(r[1].active,true);assert.equal(parseReasoningLine('ROUNDTRIP_STATUS:{}'),null);assert.equal(parseReasoningLine('ROUNDTRIP_REASONING:{'),null)});
const {parseLiveLine}=require('../extension');
test('live intermediate answers are parsed with the reasoning blocks',()=>{const r=parseLiveLine('ROUNDTRIP_REASONING:'+JSON.stringify({blocks:[{title:'Reasoned for 38s',lines:['x']},{title:'Reasoned for 4s',lines:[]}],answers:[{after:0,text:'Plan: build it.'},{after:1,text:' The 3 failures came from the tests. '},{after:9,text:'All pass.'},{after:2,text:'  '},null]}));assert.equal(r.blocks.length,2);assert.deepEqual(r.answers,[{after:0,text:'Plan: build it.'},{after:1,text:'The 3 failures came from the tests.'},{after:2,text:'All pass.'}]);assert.deepEqual(parseLiveLine('ROUNDTRIP_REASONING:'+JSON.stringify({blocks:[]})),{blocks:[],answers:[]});assert.equal(parseLiveLine('ROUNDTRIP_STATUS:{}'),null)});

test('reasoning sections carry their steps; Coding and executing steps are kept empty',()=>{const r=parseLiveLine('ROUNDTRIP_REASONING:'+JSON.stringify({blocks:[{title:'Creating file',active:true,lines:['b'],steps:[{title:'Reasoning',total:12,lines:Array.from({length:12},(_,i)=>'r'+i)},{title:'Coding and executing',total:3,lines:['print(1)','x=2','1']},{title:'Creating file',lines:['Creating file index.html']}]},{title:'Reasoned for 8m 6s',lines:[]}],answers:[{after:1,text:'Plan.'}]}));assert.equal(r.blocks[0].title,'Creating file');assert.deepEqual(r.blocks[0].steps.map(s=>s.title),['Reasoning','Coding and executing','Creating file']);assert.deepEqual(r.blocks[0].steps[1],{title:'Coding and executing',kind:'coding',total:0,lines:[],hidden:true});assert.equal(r.blocks[0].steps[0].kind,'thought');assert.equal(r.blocks[0].steps[0].lines.length,10);assert.equal(r.blocks[0].steps[0].total,12);assert.deepEqual(r.blocks[1].steps,[]);assert.equal(r.answers[0].after,1)});
test('completed titled steps stay distinct from expandable thought details',()=>{const r=parseLiveLine('ROUNDTRIP_REASONING:'+JSON.stringify({blocks:[{title:'Working through the steps',steps:[{title:'Thought process',kind:'thought',lines:['Inspecting files']},{title:'Reviewing Python comments',kind:'completed',lines:[]},{title:'Thought process',lines:['Running tests']}]}]}));assert.deepEqual(r.blocks[0].steps.map(s=>({title:s.title,kind:s.kind})),[{title:'Thought process',kind:'thought'},{title:'Reviewing Python comments',kind:'completed'},{title:'Thought process',kind:'thought'}])});
const {parseChatLine,chatTitle,normalizeChats,chatSummaries}=require('../extension');
test('saved chats continue the same Copilot chat URL',()=>{const a=buildArgs('/script.py','fix','/repo',{model:'GPT/GPT-6',timeout:900,chatUrl:'https://m365.cloud.microsoft/chat/conversation/abc',temporaryChat:true});assert.equal(a[a.indexOf('--chat-url')+1],'https://m365.cloud.microsoft/chat/conversation/abc');assert.ok(a.includes('--no-temporary-chat'));assert.equal(a[2],'fix');const n=buildArgs('/script.py','fix','/repo',{model:'GPT/GPT-6',timeout:900});assert.ok(!n.includes('--chat-url'))});
test('chat URL lines are parsed',()=>{assert.equal(parseChatLine('ROUNDTRIP_CHAT:{"url":"https://m365.cloud.microsoft/chat/conversation/abc"}'),'https://m365.cloud.microsoft/chat/conversation/abc');assert.equal(parseChatLine('ROUNDTRIP_CHAT:{"url":"javascript:alert(1)"}'),null);assert.equal(parseChatLine('ROUNDTRIP_CHAT:{'),null);assert.equal(parseChatLine('ROUNDTRIP_STATUS:{}'),null)});
test('chat history titles, order and summaries',()=>{assert.equal(chatTitle('  Add   a README  '),'Add a README');assert.equal(chatTitle('x'.repeat(80)).length,60);assert.equal(chatTitle(''),'New chat');const list=normalizeChats([{id:'a',updated:1,messages:[{role:'user',text:'p'}]},null,{id:'b',updated:5,messages:[],copilotUrl:'https://x/c'},{id:'c'}]);assert.deepEqual(list.map(c=>c.id),['b','a']);const s=chatSummaries(list,'a');assert.equal(s[0].linked,true);assert.equal(s[1].prompts,1);assert.equal(s[1].active,true)});


test('chat actions appear in the panel without duplicate view-title buttons', () => {
  const manifest = require('../package.json');
  const actions = ['copilotRoundtrip.newChat', 'copilotRoundtrip.showHistory'];
  const titleItems = manifest.contributes.menus?.['view/title'] || [];
  for (const command of actions) {
    assert.ok(manifest.contributes.commands.some(item => item.command === command));
    assert.ok(!titleItems.some(item => item.command === command));
  }
  const source = fsT.readFileSync(pathT.join(__dirname, '../extension.js'), 'utf8');
  for (const id of ['newChat', 'historyToggle']) {
    assert.equal((source.match(new RegExp('id="' + id + '"', 'g')) || []).length, 1);
  }
});

test('webview renders task plans and the working timeline as structured UI', () => {
  const source = fsT.readFileSync(pathT.join(__dirname, '../media/chat.js'), 'utf8');
  for (const name of ['task-card', 'task-row', 'task-stop', 'task-stopped-response', 'work-section', 'work-timeline', 'thought-prose', 'work-completed']) assert.ok(source.includes(name));
  assert.ok(source.includes('Working through the steps'));
  assert.ok(source.includes("detail.kind==='completed'?completedStepItem(detail):thoughtItem"));
  assert.ok(source.includes("step.append(updateAnswer(i+1,byAfter.get(i+1),lastAfter,true))"));
  assert.ok(source.includes("vscode.postMessage({type:'cancel'})"));
  assert.ok(source.includes("stopped?'Task stopped'"));
});
