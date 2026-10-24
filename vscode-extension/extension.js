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
function buildArgs(script, request, repo, cfg) {
  const args = [script];
  if (cfg.profilePath) args.push('--profile', cfg.profilePath);
  args.push('fix', request, '--repo', repo, '--model', cfg.model || 'auto', '--timeout', String(cfg.timeout || 900));
  if (cfg.headless) args.push('--headless');
  if (cfg.allowDirty) args.push('--allow-dirty');
  if (cfg.workDir) args.push('--work-dir', cfg.workDir);
  if (cfg.backupDir) args.push('--backup-dir', cfg.backupDir);
  return args;
}
function config() {
  const c = vscode.workspace.getConfiguration('copilotRoundtrip');
  return {pythonPath:c.get('pythonPath','python'), model:c.get('model','auto'), timeout:c.get('timeout',900),
    headless:c.get('headless',false), allowDirty:c.get('allowDirty',false), profilePath:c.get('profilePath',''),
    workDir:c.get('workDir',''), backupDir:c.get('backupDir','')};
}
function nonce() { return crypto.randomBytes(16).toString('hex'); }
// Keep Playwright out of the system Python and outside the installed VSIX.
// Both Sign in and Modify repository use this same managed interpreter.
let runtimeSetup;
function runSetup(executable, args, output) {
  return new Promise((resolve, reject) => {
    output.appendLine(`> ${executable} ${args.join(' ')}`);
    const child = cp.spawn(executable, args, {windowsHide:true});
    let errors = '';
    child.stdout.on('data', data => output.append(data.toString()));
    child.stderr.on('data', data => { const text=data.toString(); errors+=text; output.append(text); });
    child.on('error', reject);
    child.on('close', code => code === 0 ? resolve() : reject(new Error(`Python setup failed (exit ${code}). ${errors.trim()}`)));
  });
}
async function ensureRuntime(context, cfg, output) {
  if (runtimeSetup) return runtimeSetup;
  runtimeSetup = (async () => {
    const folder = path.join(context.globalStorageUri.fsPath, 'python-runtime');
    const python = path.join(folder, process.platform === 'win32' ? 'Scripts' : 'bin',
      process.platform === 'win32' ? 'python.exe' : 'python');
    const marker = path.join(folder, '.playwright-ready');
    if (!fs.existsSync(python)) {
      fs.mkdirSync(path.dirname(folder), {recursive:true});
      await runSetup(cfg.pythonPath, ['-m', 'venv', folder], output);
    }
    // A missing module or interrupted setup must be repaired before launching.
    let installed = false;
    try {
      await runSetup(python, ['-c', 'import playwright.sync_api'], output);
      installed = true;
    } catch (_) { /* install below */ }
    if (!installed) {
      await runSetup(python, ['-m', 'pip', 'install', 'playwright'], output);
    }
    if (!fs.existsSync(marker)) {
      await runSetup(python, ['-m', 'playwright', 'install', 'chromium'], output);
      fs.writeFileSync(marker, 'ready\n');
    }
    return python;
  })();
  try { return await runtimeSetup; }
  finally { runtimeSetup = undefined; }
}

class ChatProvider {
  constructor(context, output) { this.context=context; this.output=output; this.busy=false; this.history=[]; this.proc=null; }
  resolveWebviewView(view) {
    this.view=view;
    const w=view.webview;
    w.options={enableScripts:true,localResourceRoots:[vscode.Uri.joinPath(this.context.extensionUri,'media')]};
    const n=nonce();
    const css=w.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri,'media','chat.css'));
    const js=w.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri,'media','chat.js'));
    w.html=`<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${w.cspSource}; script-src 'nonce-${n}';"><link rel="stylesheet" href="${css}"><title>Repo Round-Trip</title></head><body><main><header><strong>Repository Round-Trip</strong><button id="login" type="button">Sign in</button></header><p class="hint">Prompts modify the selected Git repository using Copilot Chat. No inline code suggestions.</p><label for="repo">Repository</label><select id="repo"></select><section id="messages" role="log" aria-live="polite"></section><form id="form"><label for="prompt">What should change?</label><textarea id="prompt" rows="4" placeholder="Describe the repository changes..." required></textarea><div class="actions"><button id="send" type="submit">Modify repository</button><button id="cancel" type="button" disabled>Cancel</button></div></form><p id="status" role="status"></p></main><script nonce="${n}" src="${js}"></script></body></html>`;
    w.onDidReceiveMessage(async m=>{
      if(m.type==='ready') this.refresh();
      if(m.type==='login') await vscode.commands.executeCommand('copilotRoundtrip.login');
      if(m.type==='run') await this.run(m.prompt,m.repo);
      if(m.type==='cancel') this.cancel();
      if(m.type==='openResult' && typeof m.path==='string') await this.openResult(m.path);
    });
    this.refresh();
  }
  post(m) { if(this.view) this.view.webview.postMessage(m); }
  refresh() {
    const folders=(vscode.workspace.workspaceFolders||[]).filter(f=>f.uri.scheme==='file').map(f=>({name:f.name,path:f.uri.fsPath}));
    this.post({type:'repos',folders}); this.post({type:'busy',value:this.busy});
    this.post({type:'history',items:this.history});
  }
  async openResult(p) {
    // Only open ZIPs emitted by successful runs, never arbitrary paths from the webview.
    if(!this.history.some(h=>h.result===p)) return;
    if(!fs.existsSync(p)) { vscode.window.showErrorMessage('Result archive no longer exists.'); return; }
    await vscode.env.openExternal(vscode.Uri.file(p));
  }
  cancel() { if(this.proc && !this.proc.killed) { this.proc.kill(); this.post({type:'status',text:'Stopping the script...'}); } }
  async run(prompt, repo) {
    if(this.busy) return;
    if(typeof prompt!=='string' || !prompt.trim()) { this.post({type:'error',text:'Enter a prompt first.'}); return; }
    const folders=(vscode.workspace.workspaceFolders||[]).filter(f=>f.uri.scheme==='file');
    if(!folders.some(f=>f.uri.fsPath===repo)) { this.post({type:'error',text:'Select an open local workspace folder.'}); return; }
    const cfg=config();
    const script=path.join(this.context.extensionPath,'scripts','copilot_repo_roundtrip.py');
    const choice=await vscode.window.showWarningMessage(
      `Upload the committed snapshot of ${repo} to Copilot and replace its working files with the returned archive? Review the script's backup and deletion behavior before continuing.`,
      {modal:true}, 'Modify repository');
    if(choice!=='Modify repository') return;
    this.busy=true; this.post({type:'busy',value:true}); this.post({type:'status',text:'Running Copilot repository round-trip...'});
    this.post({type:'message',item:{role:'user',text:prompt}});
    this.output.show(true);
    this.output.appendLine(`\n=== Repository: ${repo} ===\nRequest: ${prompt}\n`);
    const args=buildArgs(script,prompt.trim(),repo,cfg);
    let stdout='',stderr='', cancelled=false;
    try {
      this.post({type:'status',text:'Checking Python and Playwright...'});
      const python=await ensureRuntime(this.context,cfg,this.output);
      await new Promise((resolve,reject)=>{
        const child=cp.spawn(python,args,{cwd:repo,windowsHide:false,env:process.env});
        this.proc=child;
        child.stdout.on('data',chunk=>{const s=chunk.toString();stdout+=s;this.output.append(s);});
        child.stderr.on('data',chunk=>{const s=chunk.toString();stderr+=s;this.output.append(s);});
        child.on('error',reject);
        child.on('close',(code,signal)=>{cancelled=signal!==null;code===0?resolve():reject(new Error(cancelled?'Run cancelled.':`Script exited with code ${code}. ${stderr.trim() || stdout.trim()}`));});
      });
      const result=parseResult(stdout);
      const lines=[result.chat_response || 'Repository modification completed.',result.diff_stat && `Changes:\n${result.diff_stat}`,result.status && `Git status:\n${result.status}`,result.result && `Result ZIP: ${result.result}`,result.backup && `Backup: ${result.backup}`].filter(Boolean);
      const item={role:'assistant',text:lines.join('\n\n'),result:result.result || ''};
      this.history.push({role:'user',text:prompt},{...item});
      this.post({type:'message',item});
      this.post({type:'status',text:'Completed. Review the Git diff and result archive.'});
      vscode.window.showInformationMessage('Repository round-trip completed. Review your changes.');
    } catch(e) {
      const msg=String(e.message||e);
      this.history.push({role:'user',text:prompt},{role:'error',text:msg});
      this.post({type:'message',item:{role:'error',text:msg}});
      this.post({type:'status',text:'Run failed. See the output panel.'});
    } finally {this.proc=null;this.busy=false;this.post({type:'busy',value:false});}
  }
}
function activate(context) {
  const output=vscode.window.createOutputChannel('Repo Round-Trip');
  const provider=new ChatProvider(context,output);
  context.subscriptions.push(output,vscode.window.registerWebviewViewProvider('copilotRoundtrip.chat',provider,{webviewOptions:{retainContextWhenHidden:true}}));
  context.subscriptions.push(vscode.commands.registerCommand('copilotRoundtrip.open',()=>vscode.commands.executeCommand('workbench.view.extension.copilotRoundtrip')));
  context.subscriptions.push(vscode.commands.registerCommand('copilotRoundtrip.login',async ()=>{
    const cfg=config(),script=path.join(context.extensionPath,'scripts','copilot_repo_roundtrip.py');
    output.show(true);
    let python;
    try { python=await ensureRuntime(context,cfg,output); }
    catch(e) { vscode.window.showErrorMessage('Playwright setup failed: '+String(e.message||e)); return; }
    const term=vscode.window.createTerminal(process.platform==='win32' ? {name:'Repo Round-Trip Sign In',shellPath:'powershell.exe'} : {name:'Repo Round-Trip Sign In'});
    // Use PowerShell on Windows and its call operator for quoted executable paths.
    const quote=process.platform==='win32' ? s=>'"'+s.replace(/"/g,'""')+'"' : s=>"'"+s.replace(/'/g,"'\\''")+"'";
    const args=[script]; if(cfg.profilePath) args.push('--profile',cfg.profilePath); args.push('login');
    term.show(); term.sendText((process.platform==='win32' ? '& ' : '')+[python,...args].map(quote).join(' '));
    vscode.window.showInformationMessage('Sign in using the opened Chromium window, then press Enter in the terminal.');
  }));
  context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders(()=>provider.refresh()));
}
function deactivate() {}
module.exports={activate,deactivate,parseResult,buildArgs};
