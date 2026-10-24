const test=require('node:test');const assert=require('node:assert/strict');const Module=require('node:module');const original=Module._load;Module._load=function(id,...rest){if(id==='vscode')return {};return original.call(this,id,...rest)};const {buildArgs,parseResult}=require('../extension');Module._load=original;test('argument boundaries preserve prompt',()=>{const a=buildArgs('/script.py','change "this"; echo unsafe','/repo',{model:'auto',timeout:900});assert.equal(a[2],'change "this"; echo unsafe');assert.equal(a[4],'/repo')});test('JSON result parsed',()=>assert.equal(parseResult('{"result":"/tmp/a.zip"}').result,'/tmp/a.zip'));test('invalid JSON rejected',()=>assert.throws(()=>parseResult('broken')));

const {parseStatusLine}=require('../extension');
test('structured status is parsed',()=>assert.deepEqual(parseStatusLine('ROUNDTRIP_STATUS:{"stage":"uploading","message":"Uploading..."}'),{stage:'uploading',message:'Uploading...'}));
test('non-status and malformed lines are ignored',()=>{assert.equal(parseStatusLine('warning'),null);assert.equal(parseStatusLine('ROUNDTRIP_STATUS:{'),null)});

test('model selection preserves provider/model slash',()=>{const a=buildArgs('/script.py','fix','/repo',{model:'Claude/Opus 5.5',timeout:900});assert.equal(a[a.indexOf('--model')+1],'Claude/Opus 5.5')});

const {resolveChangedPath}=require('../extension');
test('changed file paths resolve for diff links',()=>{const st=' M src/app.js\nR  old.txt -> new.txt\n?? "a b.txt"\n';const ds=' src/app.js | 2 +-\n .../deep/nested/file.js | 1 +\n';assert.equal(resolveChangedPath('src/app.js',st,ds),'src/app.js');assert.equal(resolveChangedPath('old.txt -> new.txt',st,ds),'new.txt');assert.equal(resolveChangedPath('"a b.txt"',st,ds),'a b.txt');assert.equal(resolveChangedPath('../etc/passwd',st,ds),null)});
