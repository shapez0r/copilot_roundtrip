const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function view() {
  const source = fs.readFileSync(path.join(__dirname, '../media/chat.js'), 'utf8');
  function element() {
    const el = {children: [], attributes: {}, events: {}, hidden: false,
      get textContent() { return this.children.length ? this.children.map(child => child.textContent).join('') : (this.text || ''); },
      set textContent(value) { this.children = []; this.text = value; },
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

test('summary keeps the fixed label bold and the changing step separate', () => {
  const v = view(); v.update([block('Inspecting files')]);
  const title = v.work().firstChild.children[1];
  assert.equal(title.children[0].className, 'work-title-prefix');
  assert.equal(title.children[0].textContent, 'Working through the steps: ');
  assert.equal(title.children[1].className, 'work-title-step');
  v.update([block('Testing')]);
  assert.equal(title.children[1].textContent, 'Testing');
  const css = fs.readFileSync(path.join(__dirname, '../media/chat.css'), 'utf8');
  assert.match(css, /\.work-title-prefix\{font-weight:700\}/);
  assert.match(css, /\.work-title-step\{font-weight:400\}/);
});
test('active indicators smoothly build a shared brick wall and respect reduced motion', () => {
  const css = fs.readFileSync(path.join(__dirname, '../media/chat.css'), 'utf8');
  assert.ok(!css.includes('activity-dots'));
  assert.match(css, /animation:activity-build 4.8s cubic-bezier\(\.22,1,\.36,1\) infinite/);
  const rule = css.match(/\.spinner::before,\.task-state.active::before,\.task-row-state.active::before\{([^}]+)\}/)[1];
  assert.equal((rule.match(/linear-gradient/g) || []).length, 6);
  assert.ok(rule.includes('background-repeat:no-repeat'));
  assert.ok(rule.includes('background-size:4px 3px, 4px 3px, 4px 3px, 7px 3px, 7px 3px, 8px 3px'));
  const frames = [...css.matchAll(/(\d+)%\{opacity:(\d);background-position:([^;]+);background-size:([^}]+)\}/g)];
  const frame = percent => frames.find(item => Number(item[1]) === percent);
  for (let i = 0; i < 6; i++) {
    const sizes = frame(13 + 11 * i)[4].split(',');
    assert.equal(sizes.filter(size => !size.startsWith('0px')).length, i + 1);
    assert.ok(sizes.slice(i + 1).every(size => size === '0px 3px'));
  }
  assert.equal(frame(82)[2], '1');
  assert.equal(frame(96)[2], '0');
  assert.deepEqual(frame(0).slice(2), frame(100).slice(2));
  assert.match(css, /@media \(prefers-reduced-motion:reduce\).*\.spinner::before,\.task-state.active::before,\.task-row-state.active::before\{animation:none\}/);
});


test('answers without reasoning do not create a working timeline', () => {
  const v = view();
  v.render([], [{after: 0, text: 'Repository completed.'}]);
  assert.equal(v.work().className, 'live-answer');
  assert.equal(v.work().firstChild.textContent, 'Repository completed.');
});

test('finished reasoning has no active summary while repository processing continues', () => {
  const v = view(); v.update([block('Testing', false)]);
  assert.equal(v.work().firstChild.children[0].className, 'work-state');
  assert.equal(v.work().lastChild.firstChild.firstChild.firstChild.className, 'work-step-state');
});

test('idle view clears stale active step icons', () => {
  const v = view(); v.activity.busy = false; v.update([block('Coding and executing')]);
  const step = v.work().lastChild.firstChild;
  assert.equal(step.className, 'work-step completed');
  assert.equal(step.firstChild.firstChild.className, 'work-step-state');
  assert.equal(step.firstChild.firstChild.textContent, '\u2713');
});
