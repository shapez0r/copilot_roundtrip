const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function provider() {
  const source = fs.readFileSync(path.join(__dirname, '../extension.js'), 'utf8');
  const context = {require: id => id === 'vscode' ? {} : require(id), module: {exports: {}}, setTimeout, clearTimeout};
  vm.runInNewContext(source + '\nmodule.exports.ChatProvider = ChatProvider;', context);
  const p = new context.module.exports.ChatProvider({workspaceState: {get: (_, fallback) => fallback}}, {});
  p.sent = [];
  p.post = message => p.sent.push(JSON.parse(JSON.stringify(message)));
  p.busy = true;
  p.reasoning = {blocks: [{title: 'Coding and executing', active: true}], answers: [{after: 1, text: 'Working'}]};
  return p;
}

const cancellation = 'The task was cancelled before it could finish.';

test('cancel without a task card clears coding activity and creates one stopped response', () => {
  const p = provider();
  p.cancel();
  assert.equal(p.tasks.length, 1);
  assert.equal(p.tasks[0].state, 'Task stopped');
  assert.equal(p.tasks[0].active, false);
  assert.equal(p.tasks[0].steps.length, 0);
  assert.equal(p.tasks[0].response, cancellation);
  assert.equal(p.reasoning.blocks.length, 0);
  assert.equal(p.reasoning.answers.length, 0);
  assert.equal(p.stopCurrentTask(), true);
  assert.equal(p.tasks.length, 1);
});

test('cancel preserves the task title and introduction but not duplicate cancellation text', () => {
  const p = provider();
  p.tasks = [{title: 'Build games', state: 'In progress', active: true, steps: [{title: 'Build', state: 'active'}]}];
  p.reasoning.answers = [{after: 0, text: 'I will build two games.'}, {after: 0, text: cancellation}];
  p.cancel();
  assert.equal(p.tasks[0].title, 'Build games');
  assert.equal(p.reasoning.answers.length, 1);
  assert.equal(p.reasoning.answers[0].text, 'I will build two games.');
});

test('late stream and status updates cannot restore activity after cancel', () => {
  const p = provider();
  p.cancel();
  const stopped = JSON.stringify({tasks: p.tasks, reasoning: p.reasoning, status: p.currentStatus});
  const sent = p.sent.length;
  p.updateReasoning({blocks: [{title: 'Coding and executing', active: true}], answers: [{after: 0, text: cancellation}]});
  p.updateProgress('task', 'In progress...', [{title: 'Build', active: true}]);
  p.updateProgress('log', 'Still working');
  assert.equal(JSON.stringify({tasks: p.tasks, reasoning: p.reasoning, status: p.currentStatus}), stopped);
  assert.equal(p.sent.length, sent);
  p.cancel();
  assert.equal(p.sent.length, sent);
  p.updateProgress('cancelled', cancellation);
  assert.equal(p.currentStatus, cancellation);
});

test('cancel requests graceful process exit and subsequent runs accept activity', () => {
  const p = provider();
  const child = {};
  p.proc = child;
  let requested;
  p.requestQuit = value => { requested = value; };
  p.cancel();
  assert.equal(requested, child);
  p.cancelRequested = false;
  p.resetLive();
  p.updateReasoning({blocks: [{title: 'Reasoning', active: true}], answers: []});
  p.updateProgress('waiting', 'Waiting');
  assert.equal(p.tasks.length, 0);
  assert.equal(p.reasoning.blocks[0].active, true);
  assert.equal(p.currentStatus, 'Waiting');
});
