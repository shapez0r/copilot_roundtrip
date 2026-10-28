"use strict";
const vscode = require('vscode');
const cp = require('child_process');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const os = require('os');

function parseResult(output) {
  const text = output.trim();
  // The Python script emits a single JSON object on success.
  try { return JSON.parse(text); } catch (_) {
    throw new Error('The round-trip script did not return valid JSON. See the output panel for details.');
  }
}
const STATUS_PREFIX = 'ROUNDTRIP_STATUS:';
function parseStatusLine(line) {
  if (!line.startsWith(STATUS_PREFIX)) return null;
  try {
    const value = JSON.parse(line.slice(STATUS_PREFIX.length));
    return typeof value.message === 'string' && typeof value.stage === 'string' ? value : null;
  } catch (_) { return null; }
}
const REASONING_PREFIX = 'ROUNDTRIP_REASONING:';
const REASONING_TAIL = 10;
const ANSWER_MAX_CHARS = 4000;
// Live output streamed by the script: reasoning blocks [{title, active, total, lines:[latest <=10 lines]}]
// and intermediate answers [{after, text}] shown after the first `after` reasoning blocks.
function parseLiveLine(line) {
  if (!line.startsWith(REASONING_PREFIX)) return null;
  try {
    const value = JSON.parse(line.slice(REASONING_PREFIX.length));
    if (!value || !Array.isArray(value.blocks)) return null;
    const blocks = value.blocks.filter(b => b && typeof b === 'object').map((b, i) => {
      const lines = (Array.isArray(b.lines) ? b.lines : []).map(String).slice(-REASONING_TAIL);
      return {index: i, title: String(b.title || 'Reasoning'), active: b.active === true,
        total: Math.max(Number(b.total) || 0, lines.length), lines};
    });
    const answers = (Array.isArray(value.answers) ? value.answers : [])
      .filter(a => a && typeof a === 'object' && typeof a.text === 'string' && a.text.trim())
      .map(a => {
        const text = a.text.trim();
        return {after: Math.max(0, Math.min(blocks.length, Math.floor(Number(a.after)) || 0)),
          text: text.length > ANSWER_MAX_CHARS ? '\u2026' + text.slice(-ANSWER_MAX_CHARS) : text};
      });
    return {blocks, answers};
  } catch (_) { return null; }
}
function parseReasoningLine(line) {
  const live = parseLiveLine(line);
  return live ? live.blocks : null;
}
function buildArgs(script, request, repo, cfg) {
  const args = [script];
  if (cfg.profilePath) args.push('--profile', cfg.profilePath);
  args.push('fix', request, '--repo', repo, '--model', cfg.model || 'GPT/GPT-6', '--timeout', String(cfg.timeout || 900));
  if (cfg.showBrowser) args.push('--show-browser');
  if (cfg.temporaryChat === false) args.push('--no-temporary-chat');
  if (cfg.allowDirty) args.push('--allow-dirty');
  if (cfg.workDir) args.push('--work-dir', cfg.workDir);
  if (cfg.backupDir) args.push('--backup-dir', cfg.backupDir);
  for (const file of cfg.attachments || []) args.push('--attach', file);
  return args;
}
// Map a path shown in the Changes or Git status sections to a repository-relative path.
function cleanGitPath(p) {
  let v=String(p||'').trim();
  if(v.includes(' -> ')) v=v.split(' -> ').pop().trim();
  if(v.length>1 && v.startsWith('"') && v.endsWith('"')) {
    try { v=JSON.parse(v); } catch (_) { v=v.slice(1,-1); }
  }
  return v;
}
function statPath(p) {
  let v=String(p||'').trim();
  // Renames in diff --stat: "dir/{old => new}/file" or "old => new".
  const brace=v.match(/^(.*)\{(.*) => (.*)\}(.*)$/);
  if(brace) v=(brace[1]+brace[3]+brace[4]).replace(/\/\//g,'/');
  else if(v.includes(' => ')) v=v.split(' => ').pop().trim();
  return v;
}
function resolveChangedPath(file, gitStatus, diffStat) {
  const statusPaths=String(gitStatus||'').split(/\r?\n/).filter(l=>l.trim()).map(l=>cleanGitPath(l.slice(3)||l));
  const statPaths=String(diffStat||'').split(/\r?\n/).map(l=>l.match(/^\s*(.+?)\s+\|\s+/)).filter(Boolean).map(m=>statPath(m[1]));
  const known=[...statusPaths,...statPaths];
  let wanted=cleanGitPath(statPath(file));
  if(wanted.startsWith('...')) {
    // diff --stat abbreviates long paths; match the unique known suffix.
    const suffix=wanted.slice(3);
    const matches=[...new Set(statusPaths.filter(p=>p.endsWith(suffix)))];
    return matches.length===1?matches[0]:null;
  }
  return known.includes(wanted)?wanted:null;
}
// Additional prompt attachments: picked/dropped file paths, or pasted/dropped bytes (e.g. images).
const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024;
function safeAttachmentName(name, fallback) {
  let v = path.basename(String(name || '').replace(/\\/g, '/')).replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').trim();
  if (!v || v === '.' || v === '..') v = fallback;
  return v.slice(0, 150);
}
// Returns script-ready file paths; byte attachments are written to a private temporary folder.
function materializeAttachments(items, makeDir) {
  const files = [], used = new Set();
  let dir = null;
  (Array.isArray(items) ? items : []).forEach((a, i) => {
    if (!a || typeof a !== 'object') return;
    if (a.kind === 'path' && typeof a.path === 'string') {
      const st = fs.statSync(a.path, {throwIfNoEntry: false});
      if (!st || !st.isFile()) throw new Error('Attachment not found or not a file: ' + a.path);
      if (!files.includes(a.path)) files.push(a.path);
      return;
    }
    if (a.kind === 'data' && typeof a.data === 'string') {
      const bytes = Buffer.from(a.data, 'base64');
      if (!bytes.length) throw new Error('Attachment is empty: ' + (a.name || 'file'));
      if (bytes.length > MAX_ATTACHMENT_BYTES) throw new Error('Attachment is larger than 50 MB: ' + (a.name || 'file'));
      let name = safeAttachmentName(a.name, `attachment-${i + 1}`);
      if (used.has(name.toLowerCase())) name = `${i + 1}-${name}`;
      used.add(name.toLowerCase());
      if (!dir) dir = makeDir();
      const file = path.join(dir, name);
      fs.writeFileSync(file, bytes);
      files.push(file);
    }
  });
  return {files, dir};
}
function config() {
  const c = vscode.workspace.getConfiguration('copilotRoundtrip');
  return {pythonPath:c.get('pythonPath','python'), model:c.get('model','GPT/GPT-6'), timeout:c.get('timeout',900),
    showBrowser:c.get('showBrowser',false), temporaryChat:c.get('temporaryChat',true), allowDirty:c.get('allowDirty',false), profilePath:c.get('profilePath',''),
    workDir:c.get('workDir',''), backupDir:c.get('backupDir','')};
}
// The repository is always the current workspace: the folder of the active editor, else the first local folder.
function currentRepo() {
  const editor=vscode.window.activeTextEditor;
  const active=editor ? vscode.workspace.getWorkspaceFolder(editor.document.uri) : undefined;
  if(active && active.uri.scheme==='file') return active.uri.fsPath;
  const folder=(vscode.workspace.workspaceFolders||[]).find(f=>f.uri.scheme==='file');
  return folder ? folder.uri.fsPath : null;
}
function nonce() { return crypto.randomBytes(16).toString('hex'); }
// Keep Playwright out of the system Python and outside the installed VSIX.
// Both Sign in and Modify repository use this same managed interpreter.
let runtimeSetup;
function runSetup(executable, args, output, progress) {
  return new Promise((resolve, reject) => {
    output.appendLine(`> ${executable} ${args.join(' ')}`);
    if(progress) progress('Setting up Python environment: '+args.slice(0,3).join(' '));
    const child = cp.spawn(executable, args, {windowsHide:true});
    let errors = '';
    child.stdout.on('data', data => {const text=data.toString();output.append(text);if(progress) for(const line of text.split(/\r?\n/)) if(line.trim()) progress(line.trim());});
    child.stderr.on('data', data => { const text=data.toString(); errors+=text; output.append(text);if(progress) for(const line of text.split(/\r?\n/)) if(line.trim()) progress(line.trim()); });
    child.on('error', reject);
    child.on('close', code => code === 0 ? resolve() : reject(new Error(`Python setup failed (exit ${code}). ${errors.trim()}`)));
  });
}
async function ensureRuntime(context, cfg, output, progress) {
  if (runtimeSetup) return runtimeSetup;
  runtimeSetup = (async () => {
    const folder = path.join(context.globalStorageUri.fsPath, 'python-runtime');
    const python = path.join(folder, process.platform === 'win32' ? 'Scripts' : 'bin',
      process.platform === 'win32' ? 'python.exe' : 'python');
    const marker = path.join(folder, '.playwright-ready');
    if (!fs.existsSync(python)) {
      fs.mkdirSync(path.dirname(folder), {recursive:true});
      await runSetup(cfg.pythonPath, ['-m', 'venv', folder], output, progress);
    }
    // A missing module or interrupted setup must be repaired before launching.
    let installed = false;
    try {
      await runSetup(python, ['-c', 'import playwright.sync_api'], output, progress);
      installed = true;
    } catch (_) { /* install below */ }
    if (!installed) {
      await runSetup(python, ['-m', 'pip', 'install', 'playwright'], output, progress);
    }
    if (!fs.existsSync(marker)) {
      await runSetup(python, ['-m', 'playwright', 'install', 'chromium'], output, progress);
      fs.writeFileSync(marker, 'ready\n');
    }
    return python;
  })();
  try { return await runtimeSetup; }
  finally { runtimeSetup = undefined; }
}

class ChatProvider {
  constructor(context, output) { this.context=context; this.output=output; this.busy=false; this.history=[]; this.proc=null; this.currentStatus=""; this.statusHistory=[]; this.loginProc=null; this.reasoning={blocks:[],answers:[]}; }
  resolveWebviewView(view) {
    this.view=view;
    const w=view.webview;
    w.options={enableScripts:true,localResourceRoots:[vscode.Uri.joinPath(this.context.extensionUri,'media')]};
    const n=nonce();
    const css=w.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri,'media','chat.css'));
    const js=w.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri,'media','chat.js'));
    w.html=`<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${w.cspSource}; img-src data:; script-src 'nonce-${n}';"><link rel="stylesheet" href="${css}"><title>Repo Round-Trip</title></head><body><main><header><strong>Repository Round-Trip</strong><div class="header-controls"><label for="model">Model</label><div class="model-combo" id="modelCombo"><input id="model" type="text" value="GPT/GPT-6" spellcheck="false" autocomplete="off" role="combobox" aria-autocomplete="none" aria-expanded="false" aria-controls="modelList" title="Choose a model or type any provider/model, e.g. GPT/GPT-6"><button id="modelToggle" type="button" tabindex="-1" aria-label="Show model choices" aria-haspopup="listbox" aria-expanded="false">&#9662;</button><ul id="modelList" role="listbox" aria-label="Models" hidden><li id="modelOpt0" role="option" data-value="GPT/GPT-6">GPT/GPT-6</li><li id="modelOpt1" role="option" data-value="Claude/Opus 5.5">Claude/Opus 5.5</li></ul></div><label class="browser-choice" for="temporaryChat" title="Enable Copilot Temporary chat before selecting the model"><input id="temporaryChat" type="checkbox" checked> Temporary chat</label><label class="browser-choice" for="showBrowser"><input id="showBrowser" type="checkbox"> Show browser</label><button id="login" type="button">Sign in</button><button id="finishLogin" type="button" hidden>Finish sign in</button></div></header><p class="hint">Prompts modify the current workspace Git repository using Copilot Chat. No inline code suggestions.</p><section id="messages" role="log" aria-live="polite"></section><form id="form"><label for="prompt">What should change?</label><textarea id="prompt" rows="4" placeholder="Describe the repository changes... (paste or drop images/files to attach)" required></textarea><div class="attach-bar"><button id="attach" type="button" title="Attach files to the prompt">Attach files...</button><span class="attach-hint">Paste an image, or drag and drop images/files here (hold Shift while dropping).</span></div><ul id="attachments" class="attachments" aria-label="Attached files" hidden></ul><div class="actions"><button id="send" type="submit">Modify repository</button><button id="cancel" type="button" disabled>Cancel</button></div></form><section id="progress" aria-label="Execution progress"><strong>Execution activity</strong><div id="activityLine" role="status" aria-live="polite"><span id="activitySpinner" class="spinner" aria-hidden="true" hidden></span><span id="activityText">Idle</span></div><details id="reasoning" class="reasoning" open hidden><summary><span id="reasoningSpinner" class="spinner" aria-hidden="true" hidden></span><span id="reasoningTitle">Live response</span></summary><div id="reasoningBlocks" class="reasoning-blocks" aria-live="polite"></div></details></section></main><script nonce="${n}" src="${js}"></script></body></html>`;
    w.onDidReceiveMessage(async m=>{
      if(m.type==='ready') this.refresh();
      if(m.type==='login') await vscode.commands.executeCommand('copilotRoundtrip.login');
      if(m.type==='finishLogin') this.finishLogin();
      if(m.type==='run') await this.run(m.prompt,m.model,m.showBrowser,m.attachments,m.temporaryChat);
      if(m.type==='pickAttachments') await this.pickAttachments();
      if(m.type==='attachUris' && Array.isArray(m.uris)) this.attachUris(m.uris);
      if(m.type==='cancel') this.cancel();
      if(m.type==='copy' && typeof m.text==='string') await this.copyText(m.text);
      if(m.type==='openResult' && typeof m.path==='string') await this.openResult(m.path);
      if(m.type==='openDiff' && typeof m.repo==='string' && typeof m.file==='string') await this.openDiff(m.repo,m.file);
    });
    this.refresh();
  }
  post(m) { if(this.view) this.view.webview.postMessage(m); }
  async copyText(text) {
    try { await vscode.env.clipboard.writeText(text); vscode.window.setStatusBarMessage('Copied to clipboard.',2000); }
    catch(e) { vscode.window.showErrorMessage('Could not copy to clipboard: '+String(e.message||e)); }
  }
  refresh() {
    this.post({type:'busy',value:this.busy});
    this.post({type:'history',items:this.history}); this.post({type:'progressHistory',items:this.statusHistory}); this.post({type:'reasoning',blocks:this.reasoning.blocks,answers:this.reasoning.answers}); this.post({type:'model',value:config().model}); this.post({type:'showBrowser',value:config().showBrowser}); this.post({type:'temporaryChat',value:config().temporaryChat}); this.post({type:'loginActive',value:!!this.loginProc});
  }
  async openResult(p) {
    // Only open ZIPs emitted by successful runs, never arbitrary paths from the webview.
    if(!this.history.some(h=>h.result===p)) return;
    if(!fs.existsSync(p)) { vscode.window.showErrorMessage('Result archive no longer exists.'); return; }
    await vscode.env.openExternal(vscode.Uri.file(p));
  }
  updateProgress(stage, message) {
    this.currentStatus = message;
    this.statusHistory.push({stage, message});
    if (this.statusHistory.length > 100) this.statusHistory.shift();
    this.post({type:'progressHistory', items:this.statusHistory});
  }
  updateReasoning(live) {
    this.reasoning = {blocks: live.blocks || [], answers: live.answers || []};
    this.post({type:'reasoning', blocks:this.reasoning.blocks, answers:this.reasoning.answers});
  }
  postAttachedPaths(paths) {
    const items=[];
    for(const p of paths) {
      const st=fs.statSync(p,{throwIfNoEntry:false});
      if(st && st.isFile()) items.push({kind:'path',path:p,name:path.basename(p),size:st.size});
    }
    if(items.length) this.post({type:'attached',items});
    if(items.length<paths.length) this.post({type:'error',text:'Only files can be attached (folders and missing paths were skipped).'});
  }
  async pickAttachments() {
    if(this.busy) return;
    const uris=await vscode.window.showOpenDialog({canSelectMany:true,canSelectFiles:true,canSelectFolders:false,openLabel:'Attach'});
    if(uris && uris.length) this.postAttachedPaths(uris.filter(u=>u.scheme==='file').map(u=>u.fsPath));
  }
  // Files dropped from the VS Code Explorer or editor tabs arrive as URIs.
  attachUris(list) {
    if(this.busy) return;
    const paths=[];
    for(const raw of list) {
      const v=String(raw).trim();
      // Explorer drops may provide file:// URIs or plain absolute paths (codefiles).
      if(/^[a-zA-Z]:[\\/]/.test(v) || v.startsWith('/') || v.startsWith('\\\\')) { paths.push(vscode.Uri.file(v).fsPath); continue; }
      try { const u=vscode.Uri.parse(v,true); if(u.scheme==='file') paths.push(u.fsPath); } catch (_) { /* skip */ }
    }
    if(paths.length) this.postAttachedPaths(paths);
    else this.post({type:'error',text:'Dropped item is not a local file.'});
  }
  finishLogin() { if(this.loginProc && this.loginProc.stdin.writable) { this.updateProgress('login','Finishing browser sign-in...'); this.loginProc.stdin.write('\n'); } }
  cancel() { if(this.proc && !this.proc.killed) { this.proc.kill(); this.updateProgress('cancelled','Stopping the script...'); } }
  async run(prompt, model, showBrowser, attachments, temporaryChat) {
    if(this.busy) return;
    if(typeof prompt!=='string' || !prompt.trim()) { this.post({type:'error',text:'Enter a prompt first.'}); return; }
    const repo=currentRepo();
    if(!repo) { this.post({type:'error',text:'Open a local workspace folder first.'}); return; }
    const cfg=config();
    cfg.model=typeof model==='string' ? model.trim() : '';
      if(!/^[^/\s]+\/[^/]+$/.test(cfg.model)) { this.updateProgress('error','Enter a model as provider/model (for example GPT/GPT-6).'); return; }
      cfg.showBrowser=showBrowser===true;
      cfg.temporaryChat=temporaryChat!==false;
    const script=path.join(this.context.extensionPath,'scripts','copilot_repo_roundtrip.py');
    this.busy=true; this.statusHistory=[]; this.updateReasoning({blocks:[],answers:[]}); this.post({type:'progressHistory',items:[]}); this.post({type:'busy',value:true}); this.updateProgress('starting','Starting Copilot repository round-trip...');
    const attachmentNames=(Array.isArray(attachments)?attachments:[]).filter(a=>a&&typeof a==='object').map(a=>String(a.name||(a.path?path.basename(a.path):'file')));
    const userItem={role:'user',text:prompt,attachments:attachmentNames};
    this.post({type:'message',item:userItem});
    this.output.appendLine(`\n=== Repository: ${repo} ===\nRequest: ${prompt}\n`);
    let stdout='',stderr='', cancelled=false, statusBuffer='', attachDir=null;
    try {
      const attached=materializeAttachments(attachments,()=>fs.mkdtempSync(path.join(os.tmpdir(),'copilot-roundtrip-attach-')));
      attachDir=attached.dir; cfg.attachments=attached.files;
      if(cfg.attachments.length) this.output.appendLine(`Attachments: ${cfg.attachments.join(', ')}`);
      const args=buildArgs(script,prompt.trim(),repo,cfg);
      this.updateProgress('setup','Checking Python and Playwright...');
      const python=await ensureRuntime(this.context,cfg,this.output,message=>this.updateProgress('setup',message));
      await new Promise((resolve,reject)=>{
        const child=cp.spawn(python,args,{cwd:repo,windowsHide:false,env:process.env});
        this.proc=child;
        child.stdout.on('data',chunk=>{const s=chunk.toString();stdout+=s;this.output.append(s);});
        child.stderr.on('data',chunk=>{
            const s=chunk.toString();
            statusBuffer+=s;
            const lines=statusBuffer.split(/\r?\n/);
            statusBuffer=lines.pop();
            for(const line of lines){
              // Reasoning and live-answer updates are frequent; show them live without flooding logs or errors.
              const reasoning=parseLiveLine(line);
              if(reasoning){this.updateReasoning(reasoning);continue;}
              stderr+=line+'\n';this.output.appendLine(line);
              const status=parseStatusLine(line);
              if(status) this.updateProgress(status.stage,status.message);
              else if(line.trim()) this.updateProgress('log',line.trim());
            }
          });
        child.on('error',reject);
        child.on('close',(code,signal)=>{if(statusBuffer.trim()){const reasoning=parseLiveLine(statusBuffer);if(reasoning)this.updateReasoning(reasoning);else{stderr+=statusBuffer;this.output.appendLine(statusBuffer);const last=parseStatusLine(statusBuffer);this.updateProgress(last?.stage||'log',last?.message||statusBuffer.trim());}}cancelled=signal!==null;code===0?resolve():reject(new Error(cancelled?'Run cancelled.':`Script exited with code ${code}. ${stderr.trim() || stdout.trim()}`));});
      });
      const result=parseResult(stdout);
      const item={role:'assistant',repo,text:result.chat_response || 'Repository modification completed.',diffStat:result.diff_stat || '',gitStatus:result.status || ''};
      this.history.push(userItem,{...item});
      this.post({type:'message',item});
      this.updateProgress('completed','Completed. Review the Git diff.');
      vscode.window.showInformationMessage('Repository round-trip completed. Review your changes.');
    } catch(e) {
      const msg=String(e.message||e);
      this.history.push(userItem,{role:'error',text:msg});
      this.post({type:'message',item:{role:'error',text:msg}});
      this.updateProgress('failed',msg);
    } finally {
      if(attachDir) { try { fs.rmSync(attachDir,{recursive:true,force:true}); } catch (_) { /* best effort */ } }
      this.proc=null;this.busy=false;this.post({type:'busy',value:false});
    }
  }
}
function activate(context) {
  const output=vscode.window.createOutputChannel('Repo Round-Trip');
  const provider=new ChatProvider(context,output);
  context.subscriptions.push(output,vscode.window.registerWebviewViewProvider('copilotRoundtrip.chat',provider,{webviewOptions:{retainContextWhenHidden:true}}));
  context.subscriptions.push(vscode.commands.registerCommand('copilotRoundtrip.open',()=>vscode.commands.executeCommand('workbench.view.extension.copilotRoundtrip')));
  context.subscriptions.push(vscode.commands.registerCommand('copilotRoundtrip.login',async ()=>{
    if(provider.loginProc || provider.busy) return;
    const cfg=config(),script=path.join(context.extensionPath,'scripts','copilot_repo_roundtrip.py');
    provider.updateProgress('login','Preparing browser sign-in...');
    let python;
    try { python=await ensureRuntime(context,cfg,output,message=>provider.updateProgress('setup',message)); }
    catch(e) { provider.updateProgress('failed','Playwright setup failed: '+String(e.message||e)); return; }
    const args=[script]; if(cfg.profilePath) args.push('--profile',cfg.profilePath); args.push('login');
    const child=cp.spawn(python,args,{windowsHide:false,env:{...process.env,PYTHONUNBUFFERED:'1'}});
    provider.loginProc=child;
    provider.post({type:'loginActive',value:true});
    provider.updateProgress('login','Sign in using the opened browser, then click Finish sign in here.');
    for(const stream of [child.stdout,child.stderr]) {
      let buffer='';
      stream.on('data',chunk=>{
        const text=chunk.toString(); output.append(text); buffer+=text;
        const lines=buffer.split(/\r?\n/);buffer=lines.pop();
        for(const line of lines) if(line.trim()) provider.updateProgress('login',line.trim());
      });
      stream.on('end',()=>{if(buffer.trim()) provider.updateProgress('login',buffer.trim());});
    }
    child.on('error',e=>provider.updateProgress('failed','Sign-in failed: '+e.message));
    child.on('close',code=>{
      provider.loginProc=null;provider.post({type:'loginActive',value:false});
      provider.updateProgress(code===0?'login':'failed',code===0?'Sign-in browser profile saved.':'Sign-in exited with code '+code+'.');
    });
  }));
  context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders(()=>provider.refresh()));
}
function deactivate() {}
module.exports={activate,deactivate,parseResult,parseStatusLine,parseReasoningLine,parseLiveLine,buildArgs,resolveChangedPath,materializeAttachments,safeAttachmentName};
