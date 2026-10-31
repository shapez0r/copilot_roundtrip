const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function render(item, streamed) {
  const source = fs.readFileSync(path.join(__dirname, '../media/chat.js'), 'utf8');
  const add = source.slice(source.indexOf('  function add('), source.indexOf('  // Webview clipboard'));
  function element() {
    return {children: [], append(...items) { this.children.push(...items); },
      setAttribute() {}, addEventListener() {}};
  }
  const messages = element();
  const context = {document: {createElement: element}, messages,
    $: () => ({}), copyText() {}, renderChanges: () => 'changes', renderGitStatus: () => 'status'};
  vm.createContext(context);
  vm.runInContext(add + '\nthis.add = add;', context);
  context.add(item, streamed);
  return messages.children;
}

test('streamed assistant response has no duplicate card and retains change summaries', () => {
  assert.deepEqual(render({role: 'assistant', text: 'Done', diffStat: '', gitStatus: ''}, true), ['changes', 'status']);
});

test('saved and non-streamed assistant responses retain text and Copy without a heading', () => {
  for (const streamed of [false, undefined]) {
    const [card] = render({role: 'assistant', text: 'Done'}, streamed);
    assert.equal(card.children[1].textContent, 'Done');
    assert.equal(card.children[0].children.length, 1);
    assert.equal(card.children[0].children[0].textContent, 'Copy');
  }
});

test('user and error messages remain visible with streamed answers', () => {
  for (const role of ['user', 'error']) {
    const [card] = render({role, text: 'Message'}, true);
    assert.equal(card.children[1].textContent, 'Message');
    assert.equal(card.children[0].textContent, role === 'user' ? 'You' : 'Error');
  }
});
