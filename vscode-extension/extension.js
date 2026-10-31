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
    if (typeof value.message !== 'string' || typeof value.stage !== 'string') return null;
    const status = {stage:value.stage,message:value.message};
    if (Array.isArray(value.tasks)) status.tasks=value.tasks.filter(task=>task&&typeof task==='object').map(task=>({
      title:String(task.title||'Copilot task'),state:String(task.state||'In progress'),active:task.active===true||/^in progress$/i.test(String(task.state||'')),
      steps:(Array.isArray(task.steps)?task.steps:[]).filter(step=>step&&typeof step==='object'&&String(step.title||'').trim())
        .map(step=>({title:String(step.title).trim(),state:/^(completed|active|pending|failed)$/i.test(String(step.state||''))?String(step.state).toLowerCase():'pending'}))
    }));
    return status;
  } catch (_) { return null; }
}
const REASONING_PREFIX = 'ROUNDTRIP_REASONING:';
// Time the script gets to click Stop in Copilot and exit after Cancel before it is killed.
const QUIT_GRACE_MS = 30000;
const REASONING_TAIL = 10;
const ANSWER_MAX_CHARS = 4000;
// Stream reasoning sections and interleaved answers; coding steps expose titles only.
const OMIT_STEP = /^(coding\b|executing code\b|running code\b)/i;
function parseLiveLine(line) {
  if (!line.startsWith(REASONING_PREFIX)) return null;
  try {
    const value = JSON.parse(line.slice(REASONING_PREFIX.length));
    if (!value || !Array.isArray(value.blocks)) return null;
    const blocks = value.blocks.filter(b => b && typeof b === 'object').map((b, i) => {
      const lines = (Array.isArray(b.lines) ? b.lines : []).map(String).slice(-REASONING_TAIL);
      const steps = (Array.isArray(b.steps) ? b.steps : []).filter(st => st && typeof st === 'object')
        .map(st => {
          const title = String(st.title || 'Reasoning');
          if (OMIT_STEP.test(title)) return {title, total: 0, lines: [], hidden: true};
          const stLines = (Array.isArray(st.lines) ? st.lines : []).map(String).slice(-REASONING_TAIL);
          return {title, total: Math.max(Number(st.total) || 0, stLines.length), lines: stLines};
        });
      return {index: i, title: String(b.title || 'Reasoning'), active: b.active === true,
        total: Math.max(Number(b.total) || 0, lines.length), lines, steps};
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
// Saved (non-temporary) chats report their Copilot conversation URL so follow-ups continue it.
const CHAT_PREFIX = 'ROUNDTRIP_CHAT:';
function parseChatLine(line) {
  if (!line.startsWith(CHAT_PREFIX)) return null;
  try {
    const value = JSON.parse(line.slice(CHAT_PREFIX.length));
    const url = value && typeof value.url === 'string' ? value.url.trim() : '';
    return /^https:\/\/\S+$/i.test(url) ? url : null;
  } catch (_) { return null; }
}
// Persist saved chats per workspace; never save temporary chats.
const CHATS_KEY = 'copilotRoundtrip.chats';
const CURRENT_CHAT_KEY = 'copilotRoundtrip.currentChat';
const MAX_CHATS = 100;
const MAX_SAVED_TEXT = 20000;
function chatTitle(prompt) {
  const v = String(prompt || '').replace(/\s+/g, ' ').trim();
  if (!v) return 'New chat';
  return v.length > 60 ? v.slice(0, 59).trimEnd() + '\u2026' : v;
}
function savedMessage(item) {
  const out = {...item};
  if (typeof out.text === 'string' && out.text.length > MAX_SAVED_TEXT) out.text = out.text.slice(0, MAX_SAVED_TEXT) + '\n\u2026';
  return out;
}
// Most recently updated first, at most MAX_CHATS; malformed entries are dropped.
function normalizeChats(list) {
  return (Array.isArray(list) ? list : [])
    .filter(c => c && typeof c === 'object' && typeof c.id === 'string' && Array.isArray(c.messages))
    .sort((a, b) => (Number(b.updated) || 0) - (Number(a.updated) || 0))
    .slice(0, MAX_CHATS);
}
function chatSummaries(chats, currentId) {
  return normalizeChats(chats).map(c => ({id: c.id, title: c.title || 'New chat', updated: Number(c.updated) || 0,
    model: c.model || '', prompts: c.messages.filter(m => m && m.role === 'user').length,
    linked: !!c.copilotUrl, active: c.id === currentId}));
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
  // Follow-up prompts continue the saved Copilot chat (never a temporary chat).
  if (cfg.chatUrl) args.push('--chat-url', cfg.chatUrl, '--no-temporary-chat');
  else if (cfg.temporaryChat === false) args.push('--no-temporary-chat');
  if (cfg.allowDirty) args.push('--allow-dirty');
  if (cfg.workDir) args.push('--work-dir', cfg.workDir);
  if (cfg.backupDir) args.push('--backup-dir', cfg.backupDir);
  for (const file of cfg.attachments || []) args.push('--attach', file);
  return args;
}
// Map a changed file path to a repository-relative path.
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
// Use one private Playwright interpreter for sign-in and repository changes.
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
  constructor(context, output) {
    this.context=context; this.output=output; this.busy=false; this.history=[]; this.proc=null; this.currentStatus=""; this.statusHistory=[]; this.tasks=[]; this.loginProc=null; this.reasoning={blocks:[],answers:[]};
    // The first prompt selects a saved or temporary conversation.
    this.chats=normalizeChats(context.workspaceState.get(CHATS_KEY,[]));
    this.currentId=null; this.temporary=null;
    const chat=this.chats.find(c=>c.id===context.workspaceState.get(CURRENT_CHAT_KEY,null));
    if(chat){this.currentId=chat.id;this.history=chat.messages.slice();this.temporary=false;}
  }
  currentChat() { return this.currentId ? this.chats.find(c=>c.id===this.currentId)||null : null; }
  async saveChats() {
    this.chats=normalizeChats(this.chats);
    try {
      await this.context.workspaceState.update(CHATS_KEY,this.chats);
      await this.context.workspaceState.update(CURRENT_CHAT_KEY,this.currentId);
    } catch(e) { this.output.appendLine('Could not save chat history: '+String(e.message||e)); }
    this.postChats();
  }
  postChats() {
    const chat=this.currentChat();
    this.post({type:'chats',items:chatSummaries(this.chats,this.currentId)});
    this.post({type:'chatState',id:this.currentId,title:chat?chat.title:'',temporary:this.temporary,
      started:this.history.length>0,linked:!!(chat&&chat.copilotUrl)});
  }
  resetLive() {
    this.statusHistory=[]; this.tasks=[]; this.reasoning={blocks:[],answers:[]};
    this.post({type:'progressHistory',items:[]}); this.post({type:'tasks',items:[]}); this.post({type:'reasoning',blocks:[],answers:[]});
  }
  newChat() {
    if(this.busy) return;
    this.currentId=null; this.temporary=null; this.history=[]; this.resetLive();
    this.post({type:'history',items:[]}); this.saveChats(); this.post({type:'focusPrompt'});
  }
  openChat(id) {
    if(this.busy) return;
    const chat=this.chats.find(c=>c.id===id);
    if(!chat) return;
    this.currentId=chat.id; this.temporary=false; this.history=chat.messages.slice(); this.resetLive();
    this.post({type:'history',items:this.history}); this.saveChats();
  }
  async deleteChat(id) {
    if(this.busy) return;
    const chat=this.chats.find(c=>c.id===id);
    if(!chat) return;
    const ok=await vscode.window.showWarningMessage(`Delete the chat "${chat.title}" from history? The Copilot conversation itself is not deleted.`,{modal:true},'Delete');
    if(ok!=='Delete' || this.busy) return;
    this.chats=this.chats.filter(c=>c.id!==id);
    if(this.currentId===id){this.currentId=null;this.temporary=null;this.history=[];this.resetLive();this.post({type:'history',items:[]});}
    await this.saveChats();
  }
  resolveWebviewView(view) {
    this.view=view;
    const w=view.webview;
    w.options={enableScripts:true,localResourceRoots:[vscode.Uri.joinPath(this.context.extensionUri,'media')]};
    const n=nonce();
    const css=w.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri,'media','chat.css'));
    const js=w.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri,'media','chat.js'));
    w.html=`<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${w.cspSource}; img-src data:; script-src 'nonce-${n}';"><link rel="stylesheet" href="${css}"><title>Repo Round-Trip</title></head><body><main>
<div class="chat-bar"><span id="chatTitle" class="chat-title">Sessions</span><button id="newChat" class="icon-button" type="button" title="New chat" aria-label="New chat"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></button><button id="historyToggle" class="icon-button" type="button" aria-expanded="true" aria-controls="historyPanel" title="Show chat history" aria-label="Show chat history"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 11a9 9 0 1 1 2 7M3 4v7h7M12 7v5l3 2"/></svg></button></div>

<div id="conversation" class="conversation"><section id="historyPanel" class="history-panel" aria-label="Sessions"><p id="historyEmpty" class="history-empty">No saved sessions yet. Turn off Temporary chat in Chat options to keep a conversation.</p><ul id="historyList" class="history-list"></ul></section><section id="messages" role="log" aria-label="Conversation" aria-live="polite"></section><section id="progress" aria-label="Execution progress" hidden><div id="activityLine" role="status" aria-live="polite"><span id="activitySpinner" class="spinner" aria-hidden="true" hidden></span><span id="activityText">Idle</span></div><div id="reasoning" class="reasoning" aria-label="Live response" hidden><div id="reasoningBlocks" class="reasoning-blocks" aria-live="polite"></div></div></section></div>
<div class="composer-area"><p class="composer-tip"><strong>Tip:</strong> Describe a repository change, or attach files for context.</p><form id="form"><ul id="attachments" class="attachments" aria-label="Attached files" hidden></ul><label class="sr-only" for="prompt">Describe the repository changes</label><textarea id="prompt" rows="3" placeholder="Ask for repository changes" aria-describedby="composerHelp" required></textarea><div class="composer-toolbar"><button id="attach" class="icon-button" type="button" title="Attach files (or paste an image; hold Shift to drop files)" aria-label="Attach files"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></button><div class="settings-anchor"><button id="settingsToggle" class="icon-button" type="button" aria-expanded="false" aria-controls="settingsPanel" title="Chat options" aria-label="Chat options"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/></svg></button><section id="settingsPanel" class="settings-panel" aria-label="Chat options" hidden><strong>Chat options</strong><label class="browser-choice" for="temporaryChat"><input id="temporaryChat" type="checkbox" checked> Temporary chat</label><label class="browser-choice" for="showBrowser"><input id="showBrowser" type="checkbox"> Show browser</label><div class="sign-in-actions"><button id="login" type="button">Sign in</button><button id="finishLogin" type="button" hidden>Finish sign in</button></div><p class="hint">Prompts modify the current workspace Git repository using Copilot Chat. No inline code suggestions.</p></section></div><span class="agent-label" title="Repository-wide changes">&#x2039;/&#x203A; Model</span><div class="model-combo" id="modelCombo"><label class="sr-only" for="model">Model</label><input id="model" type="text" value="GPT/GPT-6" spellcheck="false" autocomplete="off" role="combobox" aria-autocomplete="none" aria-expanded="false" aria-controls="modelList" title="Choose or type a provider/model"><button id="modelToggle" type="button" tabindex="-1" aria-label="Show model choices" aria-haspopup="listbox" aria-expanded="false"><svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m3 6 5 5 5-5"/></svg></button><ul id="modelList" role="listbox" aria-label="Models" hidden><li id="modelOpt0" role="option" data-value="GPT/GPT-6">GPT/GPT-6</li><li id="modelOpt1" role="option" data-value="GPT/GPT-6.1 Sol">GPT/GPT-6.1 Sol</li><li id="modelOpt2" role="option" data-value="Claude/Opus 5.5">Claude/Opus 5.5</li></ul></div><div class="actions"><button id="send" class="icon-button" type="submit" title="Modify repository (Enter)" aria-label="Modify repository"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20V4m-7 7 7-7 7 7"/></svg></button><button id="cancel" class="icon-button" type="button" title="Stop request" aria-label="Cancel request" disabled hidden><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1"/></svg></button></div></div></form><p id="composerHelp" class="sr-only">Enter to run. Shift+Enter for a new line. Paste images or hold Shift while dropping files to attach.</p></div></main><script nonce="${n}" src="${js}"></script></body></html>`;
    w.onDidReceiveMessage(async m=>{
      if(m.type==='ready') this.refresh();
      if(m.type==='login') await vscode.commands.executeCommand('copilotRoundtrip.login');
      if(m.type==='finishLogin') this.finishLogin();
      if(m.type==='newChat') this.newChat();
      if(m.type==='openChat' && typeof m.id==='string') this.openChat(m.id);
      if(m.type==='deleteChat' && typeof m.id==='string') await this.deleteChat(m.id);
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
    this.post({type:'history',items:this.history}); this.post({type:'progressHistory',items:this.statusHistory}); this.post({type:'tasks',items:this.tasks}); this.post({type:'reasoning',blocks:this.reasoning.blocks,answers:this.reasoning.answers}); this.post({type:'model',value:config().model}); this.post({type:'showBrowser',value:config().showBrowser}); this.post({type:'temporaryChat',value:config().temporaryChat}); this.post({type:'loginActive',value:!!this.loginProc}); this.postChats();
  }
  async openResult(p) {
    // Only open ZIPs emitted by successful runs, never arbitrary paths from the webview.
    if(!this.history.some(h=>h.result===p)) return;
    if(!fs.existsSync(p)) { vscode.window.showErrorMessage('Result archive no longer exists.'); return; }
    await vscode.env.openExternal(vscode.Uri.file(p));
  }
  updateProgress(stage, message, tasks) {
    this.currentStatus = message;
    this.statusHistory.push({stage, message});
    if (this.statusHistory.length > 100) this.statusHistory.shift();
    this.post({type:'progressHistory', items:this.statusHistory});
    if(Array.isArray(tasks)){this.tasks=tasks;this.post({type:'tasks',items:this.tasks});}
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
  // Cancel clicks Stop before exiting; kill only after the grace period.
  cancel() {
    if(!this.busy || this.cancelRequested) return;
    this.cancelRequested=true;
    this.updateProgress('cancelled','Stopping the Copilot response, then quitting...');
    if(this.proc) this.requestQuit(this.proc);
  }
  requestQuit(child) {
    try { if(child.stdin && child.stdin.writable) child.stdin.write('q\n'); } catch (_) { /* fall back to kill below */ }
    clearTimeout(this.cancelTimer);
    this.cancelTimer=setTimeout(()=>{ if(child.exitCode===null && child.signalCode===null) child.kill(); },QUIT_GRACE_MS);
  }
  async run(prompt, model, showBrowser, attachments, temporaryChat) {
    if(this.busy) return;
    if(typeof prompt!=='string' || !prompt.trim()) { this.post({type:'error',text:'Enter a prompt first.'}); return; }
    const repo=currentRepo();
    if(!repo) { this.post({type:'error',text:'Open a local workspace folder first.'}); return; }
    const cfg=config();
    cfg.model=typeof model==='string' ? model.trim() : '';
      if(!/^[^/\s]+\/[^/]+$/.test(cfg.model)) { this.updateProgress('error','Enter a model as provider/model (for example GPT/GPT-6).'); return; }
      cfg.showBrowser=showBrowser===true;
    // The first prompt fixes chat persistence; saved prompts reuse the conversation.
    let chat=this.currentChat();
    if(!chat && this.temporary===null) this.temporary=temporaryChat!==false;
    if(!chat && this.temporary===false) {
      chat={id:`${Date.now().toString(36)}-${crypto.randomBytes(4).toString('hex')}`,title:chatTitle(prompt),created:Date.now(),updated:Date.now(),repo,model:cfg.model,copilotUrl:'',messages:this.history.map(savedMessage)};
      this.chats.unshift(chat); this.currentId=chat.id;
    }
    cfg.temporaryChat=!chat;
    cfg.chatUrl=chat&&chat.copilotUrl?chat.copilotUrl:'';
    const continuing=!!cfg.chatUrl;
    const script=path.join(this.context.extensionPath,'scripts','copilot_repo_roundtrip.py');
    this.busy=true; this.cancelRequested=false; this.statusHistory=[]; this.tasks=[]; this.updateReasoning({blocks:[],answers:[]}); this.post({type:'progressHistory',items:[]}); this.post({type:'tasks',items:[]}); this.post({type:'busy',value:true}); this.updateProgress('starting','Starting Copilot repository round-trip...');
    const attachmentNames=(Array.isArray(attachments)?attachments:[]).filter(a=>a&&typeof a==='object').map(a=>String(a.name||(a.path?path.basename(a.path):'file')));
    const userItem={role:'user',text:prompt,attachments:attachmentNames};
    this.post({type:'message',item:userItem});
    if(chat && !continuing && chat.messages.some(x=>x.role==='user')) this.output.appendLine('No Copilot chat link was recorded for this chat yet; a new Copilot conversation is started for it.');
    if(chat){chat.messages.push(savedMessage(userItem));chat.updated=Date.now();chat.model=cfg.model;await this.saveChats();}
    else this.postChats();
    if(continuing) this.updateProgress('chat','Continuing the saved Copilot chat; the current repository state is attached again.');
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
        if(this.cancelRequested) this.requestQuit(child);
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
              const chatUrl=parseChatLine(line);
              if(chatUrl){this.output.appendLine('Copilot chat: '+chatUrl);if(chat&&chat.copilotUrl!==chatUrl){chat.copilotUrl=chatUrl;this.saveChats();}continue;}
              stderr+=line+'\n';this.output.appendLine(line);
              const status=parseStatusLine(line);
               if(status) this.updateProgress(status.stage,status.message,status.tasks);
              else if(line.trim()) this.updateProgress('log',line.trim());
            }
          });
        child.on('error',reject);
        child.on('close',(code,signal)=>{if(statusBuffer.trim()){const reasoning=parseLiveLine(statusBuffer);if(reasoning)this.updateReasoning(reasoning);else{stderr+=statusBuffer;this.output.appendLine(statusBuffer);const last=parseStatusLine(statusBuffer);this.updateProgress(last?.stage||'log',last?.message||statusBuffer.trim());}}cancelled=this.cancelRequested||signal!==null;code===0?resolve():reject(new Error(cancelled?'Run cancelled.':`Script exited with code ${code}. ${stderr.trim() || stdout.trim()}`));});
      });
      const result=parseResult(stdout);
      if(chat && typeof result.chat_url==='string' && /^https:\/\//i.test(result.chat_url)) chat.copilotUrl=result.chat_url;
      const item={role:'assistant',repo,text:result.chat_response || 'Repository modification completed.',diffStat:result.diff_stat || '',gitStatus:result.status || ''};
      this.history.push(userItem,{...item});
      if(chat){chat.messages.push(savedMessage(item));chat.updated=Date.now();await this.saveChats();}
      this.post({type:'message',item});
      this.updateProgress('completed','Completed. Review the Git diff.');
      vscode.window.showInformationMessage('Repository round-trip completed. Review your changes.');
    } catch(e) {
      const msg=String(e.message||e);
      this.history.push(userItem,{role:'error',text:msg});
      if(chat){chat.messages.push(savedMessage({role:'error',text:msg}));chat.updated=Date.now();await this.saveChats();}
      this.post({type:'message',item:{role:'error',text:msg}});
      this.updateProgress('failed',msg);
    } finally {
      if(attachDir) { try { fs.rmSync(attachDir,{recursive:true,force:true}); } catch (_) { /* best effort */ } }
      clearTimeout(this.cancelTimer);this.cancelTimer=null;this.cancelRequested=false;
      this.proc=null;this.busy=false;this.post({type:'busy',value:false});this.postChats();
    }
  }
}
function activate(context) {
  const output=vscode.window.createOutputChannel('Repo Round-Trip');
  const provider=new ChatProvider(context,output);
  context.subscriptions.push(output,vscode.window.registerWebviewViewProvider('copilotRoundtrip.chat',provider,{webviewOptions:{retainContextWhenHidden:true}}));
  context.subscriptions.push(vscode.commands.registerCommand('copilotRoundtrip.open',()=>vscode.commands.executeCommand('workbench.view.extension.copilotRoundtrip')));
  context.subscriptions.push(vscode.commands.registerCommand('copilotRoundtrip.newChat',()=>provider.newChat()));
  context.subscriptions.push(vscode.commands.registerCommand('copilotRoundtrip.showHistory',()=>provider.post({type:'toggleHistory'})));
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
module.exports={activate,deactivate,parseResult,parseStatusLine,parseReasoningLine,parseLiveLine,parseChatLine,chatTitle,normalizeChats,chatSummaries,buildArgs,resolveChangedPath,materializeAttachments,safeAttachmentName};
