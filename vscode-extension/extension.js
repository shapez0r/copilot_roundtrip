"use strict";
const vscode = require('vscode');
const cp = require('child_process');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');

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
function buildArgs(script, request, repo, cfg) {
  const args = [script];
  if (cfg.profilePath) args.push('--profile', cfg.profilePath);
  args.push('fix', request, '--repo', repo, '--model', cfg.model || 'GPT/GPT-6', '--timeout', String(cfg.timeout || 900));
  if (cfg.showBrowser) args.push('--show-browser');
  if (cfg.allowDirty) args.push('--allow-dirty');
  if (cfg.workDir) args.push('--work-dir', cfg.workDir);
  if (cfg.backupDir) args.push('--backup-dir', cfg.backupDir);
  return args;
}
function config() {
  const c = vscode.workspace.getConfiguration('copilotRoundtrip');
  return {pythonPath:c.get('pythonPath','python'), model:c.get('model','GPT/GPT-6'), timeout:c.get('timeout',900),
    showBrowser:c.get('showBrowser',false), allowDirty:c.get('allowDirty',false), profilePath:c.get('profilePath',''),
    workDir:c.get('workDir',''), backupDir:c.get('backupDir','')};
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
  constructor(context, output) { this.context=context; this.output=output; this.busy=false; this.history=[]; this.proc=null; this.currentStatus=""; this.statusHistory=[]; this.loginProc=null; }
  resolveWebviewView(view) {
    this.view=view;
    const w=view.webview;
    w.options={enableScripts:true,localResourceRoots:[vscode.Uri.joinPath(this.context.extensionUri,'media')]};
    const n=nonce();
    const css=w.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri,'media','chat.css'));
    const js=w.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri,'media','chat.js'));
    w.html=`<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${w.cspSource}; script-src 'nonce-${n}';"><link rel="stylesheet" href="${css}"><title>Repo Round-Trip</title></head><body><main><header><strong>Repository Round-Trip</strong><div class="header-controls"><label for="model">Model</label><input id="model" type="text" value="GPT/GPT-6" spellcheck="false" title="Enter provider/model, e.g. GPT/GPT-6"><label class="browser-choice" for="showBrowser"><input id="showBrowser" type="checkbox"> Show browser</label><button id="login" type="button">Sign in</button><button id="finishLogin" type="button" hidden>Finish sign in</button></div></header><p class="hint">Prompts modify the selected Git repository using Copilot Chat. No inline code suggestions.</p><label for="repo">Repository</label><select id="repo"></select><section id="messages" role="log" aria-live="polite"></section><form id="form"><label for="prompt">What should change?</label><textarea id="prompt" rows="4" placeholder="Describe the repository changes..." required></textarea><div class="actions"><button id="send" type="submit">Modify repository</button><button id="cancel" type="button" disabled>Cancel</button></div></form><p id="status" role="status"></p><section id="progress" aria-label="Execution progress"><strong>Execution activity</strong><div id="activityLine" role="status" aria-live="polite"><span id="activitySpinner" class="spinner" aria-hidden="true" hidden></span><span id="activityText">Idle</span></div></section></main><script nonce="${n}" src="${js}"></script></body></html>`;
    w.onDidReceiveMessage(async m=>{
      if(m.type==='ready') this.refresh();
      if(m.type==='login') await vscode.commands.executeCommand('copilotRoundtrip.login');
      if(m.type==='finishLogin') this.finishLogin();
      if(m.type==='run') await this.run(m.prompt,m.repo,m.model,m.showBrowser);
      if(m.type==='cancel') this.cancel();
      if(m.type==='openResult' && typeof m.path==='string') await this.openResult(m.path);
    });
    this.refresh();
  }
  post(m) { if(this.view) this.view.webview.postMessage(m); }
  refresh() {
    const folders=(vscode.workspace.workspaceFolders||[]).filter(f=>f.uri.scheme==='file').map(f=>({name:f.name,path:f.uri.fsPath}));
    this.post({type:'repos',folders}); this.post({type:'busy',value:this.busy});
    this.post({type:'history',items:this.history}); this.post({type:'progressHistory',items:this.statusHistory}); this.post({type:'model',value:config().model}); this.post({type:'showBrowser',value:config().showBrowser}); this.post({type:'loginActive',value:!!this.loginProc});
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
    this.post({type:'status', text:message});
    this.post({type:'progressHistory', items:this.statusHistory});
  }
  finishLogin() { if(this.loginProc && this.loginProc.stdin.writable) { this.updateProgress('login','Finishing browser sign-in...'); this.loginProc.stdin.write('\n'); } }
  cancel() { if(this.proc && !this.proc.killed) { this.proc.kill(); this.updateProgress('cancelled','Stopping the script...'); } }
  async run(prompt, repo, model, showBrowser) {
    if(this.busy) return;
    if(typeof prompt!=='string' || !prompt.trim()) { this.post({type:'error',text:'Enter a prompt first.'}); return; }
    const folders=(vscode.workspace.workspaceFolders||[]).filter(f=>f.uri.scheme==='file');
    if(!folders.some(f=>f.uri.fsPath===repo)) { this.post({type:'error',text:'Select an open local workspace folder.'}); return; }
    const cfg=config();
    cfg.model=typeof model==='string' ? model.trim() : '';
      if(!/^[^/\s]+\/[^/]+$/.test(cfg.model)) { this.updateProgress('error','Enter a model as provider/model (for example GPT/GPT-6).'); return; }
      cfg.showBrowser=showBrowser===true;
    const script=path.join(this.context.extensionPath,'scripts','copilot_repo_roundtrip.py');
    this.busy=true; this.statusHistory=[]; this.post({type:'progressHistory',items:[]}); this.post({type:'busy',value:true}); this.updateProgress('starting','Starting Copilot repository round-trip...');
    this.post({type:'message',item:{role:'user',text:prompt}});
    this.output.appendLine(`\n=== Repository: ${repo} ===\nRequest: ${prompt}\n`);
    const args=buildArgs(script,prompt.trim(),repo,cfg);
    let stdout='',stderr='', cancelled=false, statusBuffer='';
    try {
      this.updateProgress('setup','Checking Python and Playwright...');
      const python=await ensureRuntime(this.context,cfg,this.output,message=>this.updateProgress('setup',message));
      await new Promise((resolve,reject)=>{
        const child=cp.spawn(python,args,{cwd:repo,windowsHide:false,env:process.env});
        this.proc=child;
        child.stdout.on('data',chunk=>{const s=chunk.toString();stdout+=s;this.output.append(s);});
        child.stderr.on('data',chunk=>{
            const s=chunk.toString();stderr+=s;this.output.append(s);
            statusBuffer+=s;
            const lines=statusBuffer.split(/\r?\n/);
            statusBuffer=lines.pop();
            for(const line of lines){
              const status=parseStatusLine(line);
              if(status) this.updateProgress(status.stage,status.message);
              else if(line.trim()) this.updateProgress('log',line.trim());
            }
          });
        child.on('error',reject);
        child.on('close',(code,signal)=>{if(statusBuffer.trim()){const last=parseStatusLine(statusBuffer);this.updateProgress(last?.stage||'log',last?.message||statusBuffer.trim());}cancelled=signal!==null;code===0?resolve():reject(new Error(cancelled?'Run cancelled.':`Script exited with code ${code}. ${stderr.trim() || stdout.trim()}`));});
      });
      const result=parseResult(stdout);
      const lines=[result.chat_response || 'Repository modification completed.',result.diff_stat && `Changes:\n${result.diff_stat}`,result.status && `Git status:\n${result.status}`,].filter(Boolean);
      const item={role:'assistant',text:lines.join('\n\n')};
      this.history.push({role:'user',text:prompt},{...item});
      this.post({type:'message',item});
      this.updateProgress('completed','Completed. Review the Git diff.');
      vscode.window.showInformationMessage('Repository round-trip completed. Review your changes.');
    } catch(e) {
      const msg=String(e.message||e);
      this.history.push({role:'user',text:prompt},{role:'error',text:msg});
      this.post({type:'message',item:{role:'error',text:msg}});
      this.updateProgress('failed',msg);
    } finally {this.proc=null;this.busy=false;this.post({type:'busy',value:false});}
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
module.exports={activate,deactivate,parseResult,parseStatusLine,buildArgs};
