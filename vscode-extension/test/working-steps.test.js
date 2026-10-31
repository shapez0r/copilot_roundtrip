const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function view() {
  const source = fs.readFileSync(path.join(__dirname, '../media/chat.js'), 'utf8');
  function element() {
    const el = {children: [], attributes: {}, events: {}, textContent: '', hidden: false,
      setAttribute(key, value) { this.attributes[key] = value; },
      addEventListener(key, fn) { this.events[key] = fn; },
      append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } },
      replaceChildren(...items) { this.children = []; this.append(...items); },
      contains(item) { return this.children.some(child => child === item || child.contains(item)); },
      insertBefore(item, before) {
        if (item.parent) item.remove();
        const index = before ? this.children.indexOf(before) : this.children.length;
        this.children.splice(index, 0, item); item.parent = this;
      },
      remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); },
      classList: {add() {}, remove() {}, toggle() {}}};
    Object.defineProperties(el, {firstChild: {get: () => el.children[0]}, lastChild: {get: () => el.children.at(-1)}});
    return el;
  }
  const elements = {reasoning: element(), reasoningBlocks: element()};
  const context = {document: {createElement: element}, $: id => elements[id],
    activity: {busy: true}, lastReasoning: [], lastAnswers: [], lastTasks: [], vscode: {postMessage() {}},
    span(cls, text) { const el = element(); el.className = cls; el.textContent = text; return el; }};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  // Mirror Copilot'), source.indexOf("  window.addEventListener('message'")) +
    '\nthis.render = renderReasoning; this.summary = workSummary;', context);
  context.update = blocks => { context.lastReasoning = blocks; context.render(blocks, []); };
  context.work = () => elements.reasoningBlocks.firstChild;
  return context;
}
const block = (title, active = true, steps = []) => ({title, active, steps});
const done = title => ({title, kind: 'completed'});

test('working steps start hidden with an accessible Show steps control and spinner', () => {
  const v = view(); v.update([block('Inspecting files')]);
  const work = v.work(), head = work.firstChild;
  assert.equal(work.lastChild.hidden, true);
  assert.equal(head.attributes['aria-expanded'], 'false');
  assert.equal(head.attributes['aria-controls'], work.lastChild.id);
  assert.equal(head.children[2].textContent, 'Show steps');
  assert.equal(head.children[0].className, 'work-state spinner');
  assert.equal(head.children[1].textContent, 'Working through the steps: Inspecting files');
});

test('summary advances on completed steps and holds while the next step runs', () => {
  const v = view();
  v.update([block('Reasoning', true, [done('Inspecting files')])]);
  assert.equal(v.work().firstChild.children[1].textContent, 'Working through the steps: Inspecting files');
  v.update([block('Reasoning', true, [done('Inspecting files'), {title: 'Testing', kind: 'thought', lines: ['Running']}])]);
  assert.equal(v.work().firstChild.children[1].textContent, 'Working through the steps: Inspecting files');
  v.update([block('Reasoning', true, [done('Inspecting files'), done('Testing')])]);
  assert.equal(v.work().firstChild.children[1].textContent, 'Working through the steps: Testing');
});

test('completed block title is retained until the next block completes', () => {
  const v = view();
  assert.equal(v.summary([block('Inspecting files', false), block('Testing')]), 'Inspecting files');
  assert.equal(v.summary([block('Inspecting files', false), block('Testing', false)]), 'Testing');
  assert.equal(v.summary([block('Reasoned for 1m', false, [done('Testing'), {title: 'Thought process', kind: 'thought', lines: ['Done']}])]), 'Testing');
});

test('Show and Hide steps preserve user choice across updates and reset for a new timeline', () => {
  const v = view(); v.update([block('Inspecting files')]);
  v.work().firstChild.events.click();
  assert.equal(v.work().lastChild.hidden, false);
  assert.equal(v.work().firstChild.children[2].textContent, 'Hide steps');
  v.update([block('Testing')]);
  assert.equal(v.work().lastChild.hidden, false);
  v.work().firstChild.events.click();
  assert.equal(v.work().lastChild.hidden, true);
  v.work().firstChild.events.click();
  v.update([]); v.update([block('Packaging')]);
  assert.equal(v.work().lastChild.hidden, true);
});

test('completion stops the summary spinner without hiding the disclosure control', () => {
  const v = view(); v.update([block('Testing')]);
  v.activity.busy = false; v.update([block('Testing', false)]);
  assert.equal(v.work().firstChild.children[0].className, 'work-state');
  assert.equal(v.work().firstChild.children[0].textContent, '\u2713');
  assert.equal(v.work().firstChild.children[2].textContent, 'Show steps');
});

test('summary animation respects reduced motion', () => {
  const css = fs.readFileSync(path.join(__dirname, '../media/chat.css'), 'utf8');
  assert.match(css, /\.work-title\.rotate\{animation:activity-rotate/);
  assert.match(css, /@media \(prefers-reduced-motion:reduce\).*\.work-title\.rotate\{animation:none\}/);
});
