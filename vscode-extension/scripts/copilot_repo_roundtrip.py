#!/usr/bin/env python3
"""Experimental M365 Copilot Chat browser automation for a local Git worktree.

It archives the Git index (therefore excludes .git and untracked files), uploads the
ZIP bytes under a .txt filename, sends a repair prompt, waits for a downloaded file,
validates the returned archive, and replaces the worktree after a backup.

UI automation is unsupported and selectors can change. Run `login` once, then `fix`.
"""
from __future__ import annotations
import argparse, atexit, base64, hashlib, json, os, re, shutil, subprocess, sys, tempfile, threading, time, zipfile
from pathlib import Path, PurePosixPath

DEFAULT_URL = "https://m365.cloud.microsoft/chat/"
BASE_PROMPT = (
    "Here is my repository archive. The attached .txt file contains ZIP bytes. "
    "Rename it to .zip and extract it. Make only the requested fixes, preserve all other files, "
    "then return the complete fixed repository as a downloadable ZIP file. "
    "Do not include a .git directory. To fix: {request}"
)
# Follow-up prompts in a saved (non-temporary) chat continue the same Copilot conversation. The
# Copilot sandbox is erased between prompts, so every prompt supplies the current repository state.
FOLLOWUP_PROMPT = (
    "Here is the current state of my repository archive for this follow-up request. It supersedes any "
    "earlier archive in this chat (your sandbox was reset, so earlier files are no longer available). "
    "The attached .txt file contains ZIP bytes. "
    "Rename it to .zip and extract it. Make only the requested fixes, preserve all other files, "
    "then return the complete fixed repository as a downloadable ZIP file. "
    "Do not include a .git directory. To fix: {request}"
)


ATTACHMENTS_NOTE = (
    "\n\nAdditional files are attached as context for this request "
    "(they are not part of the repository): {names}"
)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic"}


def resolve_attachments(paths):
    """Validate extra files (images, documents, ...) to attach alongside the archive."""
    result = []
    for raw in paths or []:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"Attachment not found or not a file: {raw}")
        if path not in result:
            result.append(path)
    return result


CHAT_PREFIX = "ROUNDTRIP_CHAT:"


def emit_chat(url):
    """Report the saved Copilot chat URL so later prompts can continue the same conversation."""
    print(CHAT_PREFIX + json.dumps({"url": url}), file=sys.stderr, flush=True)


def validate_chat_url(url):
    """A chat to continue must be an https Copilot conversation URL."""
    from urllib.parse import urlparse
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or not parsed.netloc:
        raise SystemExit(f"Invalid --chat-url (an https Copilot chat URL is required): {url}")
    return url


def conversation_url(current, base):
    """The page URL once Copilot has created a saved conversation, or None while it is still the start page."""
    from urllib.parse import urlparse
    cur, start = urlparse(current or ""), urlparse(base or "")
    if cur.scheme != "https" or not cur.netloc or cur.netloc != start.netloc:
        return None
    if (cur.path.rstrip("/"), cur.query, cur.fragment) == (start.path.rstrip("/"), start.query, start.fragment):
        return None
    return current


def emit_status(stage, message):
    """Line-delimited progress on stderr; stdout remains the final JSON result."""
    print("ROUNDTRIP_STATUS:" + json.dumps({"stage": stage, "message": message}),
          file=sys.stderr, flush=True)

# Time given to the Copilot web UI to finish rendering after navigation (visible and hidden browser).
UI_SETTLE_MS = 2500
# Number of most recent reasoning lines sent to the extension for each reasoning block.
REASONING_TAIL_LINES = 10
REASONING_PREFIX = "ROUNDTRIP_REASONING:"
# Maximum characters kept (latest part) of each live intermediate answer segment.
ANSWER_MAX_CHARS = 4000
# Selectors for Copilot assistant messages (live answers and the final chat response).
ASSISTANT_SELECTORS = (
    '[data-message-author-role="assistant"]',
    '[data-role="assistant"]',
    '[data-testid*="assistant-message" i]',
    '[data-testid*="copilot-message" i]',
    '[class*="assistant-message" i]',
)


def emit_reasoning(blocks, answers=None):
    """Stream the latest reasoning blocks and intermediate answers on stderr (one JSON line per change).

    blocks: [{"title", "active", "total", "lines", "steps": [{"title", "total", "lines"}]}] - one per
    reasoning section; "title" is the current step name while running, "Reasoned for ..." when done.

    answers: [{"after": N, "text": ...}] - live answer text shown after the first N reasoning blocks
    (after=0 is text before the first block), as in "answer / Reasoned for 38s / answer / ...".
    """
    print(REASONING_PREFIX + json.dumps({"blocks": blocks, "answers": answers or []}),
          file=sys.stderr, flush=True)


# Screen-reader labels Copilot puts in front of messages ("Copilot said:", "You said:").
_SPEAKER_LABEL = re.compile(r"^\s*(copilot|you)\s+said\s*:?\s*", re.I)


def strip_speaker_label(text):
    "Remove a leading 'Copilot said:' style label; a label-only text becomes empty."
    text = (text or "").strip()
    while True:
        stripped = _SPEAKER_LABEL.sub("", text, count=1).strip()
        if stripped == text:
            return text
        text = stripped


def merge_live(prev_blocks, prev_answers, blocks, answers):
    """Keep already streamed output when Copilot re-renders its final message.

    When a response finishes, Copilot collapses the reasoning blocks and re-renders the
    message, so a late read can return empty reasoning lines or only a "Copilot said:" label.
    Previously seen reasoning lines and longer answer text are kept instead of being lost.
    """
    prev_blocks = prev_blocks or []
    merged_blocks = []
    for i, block in enumerate(blocks or []):
        block = dict(block)
        old = prev_blocks[i] if i < len(prev_blocks) else None
        if old and not block.get("lines") and old.get("lines"):
            block["lines"] = list(old["lines"])
            block["total"] = max(int(old.get("total") or 0), len(old["lines"]))
        # Steps ("Reasoning", "Creating file", ...) already seen are kept the same way.
        old_steps = (old or {}).get("steps") or []
        new_steps = block.get("steps") or []
        if len(new_steps) < len(old_steps):
            new_steps = new_steps + [dict(s) for s in old_steps[len(new_steps):]]
        steps = []
        for k, item in enumerate(new_steps):
            item = dict(item)
            prev = old_steps[k] if k < len(old_steps) else None
            if prev and prev.get("title") == item.get("title") and not item.get("lines") and prev.get("lines"):
                item["lines"] = list(prev["lines"])
                item["total"] = max(int(prev.get("total") or 0), len(prev["lines"]))
            steps.append(item)
        block["steps"] = steps
        merged_blocks.append(block)
    # Blocks that temporarily vanished from the screen are kept as well.
    for old in prev_blocks[len(merged_blocks):]:
        merged_blocks.append(dict(old, active=False))
    texts = {}
    for answer in prev_answers or []:
        texts[answer["after"]] = answer["text"]
    current = {}
    for answer in answers or []:
        text = strip_speaker_label(answer.get("text"))
        if not text:
            continue
        after = answer.get("after", 0)
        old = texts.get(after, "")
        current[after] = text
        # The latest on-screen text wins (a short placeholder such as "Checking that now..." is replaced
        # by the interim answer shown later at that position); only a truncated re-render of the
        # same text keeps the longer text already streamed.
        if not (len(text) < len(old) and text.lstrip("\u2026") in old):
            texts[after] = text
    # Text that moved to another position on screen is not shown twice.
    for after in list(texts):
        if after not in current and any(texts[after] == t for a, t in current.items() if a != after):
            del texts[after]
    merged_answers = [{"after": after, "text": texts[after]} for after in sorted(texts) if texts[after]]
    return merged_blocks, merged_answers


# Quit handling: pressing Q in the terminal, or a "q" line on stdin (sent by the VS Code
# extension's Cancel button), requests a quit. Once the prompt may have been submitted, the
# Copilot response is stopped (Stop button clicked) before the browser closes and the script exits.
QUIT_EXIT_CODE = 130
_quit_event = threading.Event()


class QuitRequested(Exception):
    "Raised at a safe point after Q / Cancel was requested."


def quit_requested():
    return _quit_event.is_set()


def raise_if_quit():
    if _quit_event.is_set():
        raise QuitRequested()


def _quit_key(ch):
    return isinstance(ch, str) and ch.strip().lower() == "q"


def start_quit_listener():
    "Watch stdin for Q (terminal key press, no Enter needed) or a q/quit/cancel line (pipe)."
    stdin = sys.stdin
    if stdin is None:
        return False
    try:
        tty_input = stdin.isatty()
    except Exception:
        tty_input = False

    def windows_console():
        import msvcrt
        while not _quit_event.is_set():
            if msvcrt.kbhit():
                if _quit_key(msvcrt.getwch()):
                    _quit_event.set()
            else:
                time.sleep(0.1)

    def posix_terminal():
        import select, termios, tty
        fd = stdin.fileno()
        old = termios.tcgetattr(fd)
        restore = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, old)
        atexit.register(restore)
        tty.setcbreak(fd)
        try:
            while not _quit_event.is_set():
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready and _quit_key(os.read(fd, 1).decode("utf-8", "ignore")):
                    _quit_event.set()
        finally:
            restore()

    def pipe_lines():
        while not _quit_event.is_set():
            line = stdin.readline()
            if not line:
                return  # EOF: no quit requests can arrive any more.
            if line.strip().lower() in ("q", "quit", "cancel", "stop"):
                _quit_event.set()

    def worker():
        try:
            if tty_input and os.name == "nt":
                windows_console()
            elif tty_input:
                posix_terminal()
            else:
                pipe_lines()
        except Exception:
            pass

    threading.Thread(target=worker, name="quit-listener", daemon=True).start()
    return tty_input


def pause(page, ms):
    "page.wait_for_timeout that returns early (raising QuitRequested) when a quit is requested."
    end = time.monotonic() + ms / 1000
    while True:
        raise_if_quit()
        left = end - time.monotonic()
        if left <= 0:
            return
        page.wait_for_timeout(min(250, max(1, int(left * 1000))))


def stop_generation(page, sel, timeout_seconds=15):
    "Click Copilot's Stop button for the running response and wait until it is gone."
    stop = page.locator(sel["stop"])

    def visible_buttons():
        try:
            return [stop.nth(i) for i in range(stop.count()) if stop.nth(i).is_visible()]
        except Exception:
            return []

    started = time.monotonic()
    clicked, last_click = False, 0.0
    while time.monotonic() - started < timeout_seconds:
        buttons = visible_buttons()
        now = time.monotonic()
        if not buttons:
            if clicked:
                return True
            # The Stop button can take a moment to replace Send right after submission.
            if now - started >= 3:
                return False
        elif not clicked or now - last_click >= 3:
            try:
                buttons[-1].click(timeout=3000)
                clicked, last_click = True, now
                emit_status("cancelled", "Clicked Stop in Copilot Chat.")
            except Exception:
                pass
        page.wait_for_timeout(300)
    return clicked


def wait_for_ui(page, ms=UI_SETTLE_MS):
    """Let the Copilot UI load properly before interacting with it."""
    emit_status("browser", f"Letting Copilot UI load ({ms / 1000:g}s)...")
    page.wait_for_timeout(ms)


# Collects (1) a cheap signature of the visible chat text, used to detect on-screen output,
# and (2) the reasoning sections, as in the Copilot web UI: while a section runs its header shows
# the current step name ("Reasoning", "Creating file", "Coding and executing", ...); when it
# finishes the header becomes "Reasoned for 8m 6s". Each section holds steps (step title + text).
# Collapsed sections are expanded once per header title so their steps can be read in both
# visible and hidden browser modes. (3) The live intermediate answers: the latest assistant
# message's text split at those sections. Code blocks are left out everywhere, and the
# "Coding and executing" steps (code and its output only) are omitted from the steps.
SCREEN_SCRIPT = r"""
async (args) => {
  const tail = args.tail, custom = args.selector;
  const titleRe = /^\s*(reasoning|reasoned|thinking|thought|thought for)\b/i;
  // Section headers also show the current step name while running ("Creating file",
  // "Coding and executing", "Searching the web", ...): a short "-ing" phrase without punctuation.
  const stepRe = /^[A-Z][a-z]+ing(?:\s+(?:and\s+)?[a-z][a-z-]*){0,3}$/;
  const doneRe = /^\s*(reasoned|thought|worked)\s+for\b/i;
  // "Coding and executing" steps only hold code and its output, which are not shown.
  const omitRe = /^(coding\b|executing code\b|running code\b)/i;
  const lineMax = 2000, maxSteps = 30;
  const visible = el => !!el && el.getClientRects().length > 0;
  const textOf = el => (el && (el.innerText || el.textContent) || '').trim();
  // Screen-reader-only text (visually hidden status/live regions such as a repeated "Checking that
  // now...") is not shown in the web UI, so it is never streamed as an answer.
  const srOnly = (el, cs) => {
    if (cs.position !== 'absolute' && cs.position !== 'fixed') return false;
    if (/rect\(\s*0(px)?[\s,]+0(px)?[\s,]+0(px)?[\s,]+0(px)?\s*\)/.test(cs.clip || '') ||
        /inset\(\s*50%/.test(cs.clipPath || '')) return true;
    const r = el.getBoundingClientRect();
    return (r.width <= 1 || r.height <= 1) && /hidden|clip/.test(cs.overflow || '');
  };
  // Code blocks (e.g. under "Coding and executing") are not streamed: only the step titles and
  // prose are shown, never the code itself. Inline `code` inside sentences is kept.
  const codeSel = 'pre, [class*="code-block" i], [class*="codeblock" i], [class*="code_block" i]';
  const isCode = el => {
    if (!el || el.nodeType !== 1) return false;
    const tag = el.tagName.toUpperCase();
    if (tag === 'PRE') return true;
    if (tag === 'CODE') {
      const d = getComputedStyle(el).display;
      return !d.startsWith('inline') && d !== 'contents';
    }
    try { return el.matches(codeSel); } catch (_) { return false; }
  };
  // Visible text of an element without its code blocks (and without `exclude`, e.g. a <summary>).
  const proseOf = (el, exclude) => {
    if (!el) return '';
    let out = '';
    const skipTags = /^(SCRIPT|STYLE|NOSCRIPT|SVG|TEMPLATE|IFRAME|CANVAS|TEXTAREA|INPUT|SELECT)$/;
    const walk = node => {
      if (node.nodeType === 3) { out += node.nodeValue.replace(/\s+/g, ' '); return; }
      if (node.nodeType !== 1) return;
      if (node === exclude || isCode(node) || skipTags.test(node.tagName.toUpperCase()) ||
          node.getAttribute('aria-hidden') === 'true') return;
      const cs = getComputedStyle(node);
      if (cs.display === 'none' || cs.visibility === 'hidden' || srOnly(node, cs)) return;
      if (node.tagName === 'BR') { out += '\n'; return; }
      // Buttons (e.g. a step's expander) are kept on their own line so step names stay separate.
      const block = !cs.display.startsWith('inline') && cs.display !== 'contents' ||
        node.tagName === 'BUTTON' || node.getAttribute('role') === 'button';
      if (block) out += '\n';
      for (const child of node.childNodes) walk(child);
      if (block) out += '\n';
    };
    walk(el);
    return out.split('\n').map(l => l.replace(/[ \t\u00a0]+/g, ' ').trim()).filter(Boolean).join('\n');
  };
  let customHeads = null;
  if (custom) { try { customHeads = Array.from(document.querySelectorAll(custom)); } catch (_) { customHeads = null; } }
  let heads = customHeads || Array.from(document.querySelectorAll('button, [role="button"], summary, [aria-expanded]'));
  const headTitle = h => textOf(h).split('\n')[0].replace(/[\s>\u203a\u00bb\u2304\u2335\u02c5\u25be\u25b8]+$/, '').trim();
  const chrome = 'form, nav, header, footer, [role="toolbar"], [role="menu"], [role="menubar"], [role="dialog"], [role="tablist"]';
  // Elements of earlier turns (marked before this prompt was sent) belong to previous answers.
  const prior = el => el.hasAttribute('data-roundtrip-prior') ||
    !!(el.parentElement && el.parentElement.closest('[data-roundtrip-prior-msg]'));
  heads = heads.filter(h => {
    const t = textOf(h);
    if (prior(h)) return false;
    if (!visible(h) || !t || t.length > 120) return false;
    if (customHeads) return true;
    if (titleRe.test(t)) return true;
    // Step-name headers ("Creating file") must be expandable and outside the page chrome.
    const expandable = h.tagName === 'SUMMARY' || h.hasAttribute('aria-expanded') || !!h.querySelector('[aria-expanded]');
    let inChrome = false;
    try { inChrome = !!h.closest(chrome); } catch (_) { inChrome = false; }
    return expandable && !inChrome && stepRe.test(headTitle(h));
  });
  // Keep the outermost header when several nested elements match the same title.
  heads = heads.filter(h => !heads.some(o => o !== h && o.contains(h)));
  // Expandable steps inside an expanded section (e.g. "Coding and executing" with its code) sit
  // deeper in the DOM than the section headers: they belong to a section, they are not sections.
  const depth = el => { let d = 0; for (let e = el; e; e = e.parentElement) d++; return d; };
  const nestedIn = (h, o) => {
    if (depth(o) >= depth(h) || !(o.compareDocumentPosition(h) & Node.DOCUMENT_POSITION_FOLLOWING)) return false;
    let anc = o.parentElement;
    for (let k = 0; k < 4 && anc; k++, anc = anc.parentElement) if (anc.contains(h)) return true;
    return false;
  };
  if (!customHeads) heads = heads.filter(h => !heads.some(o => o !== h && nestedIn(h, o)));
  const expandedState = h => {
    if (h.tagName === 'SUMMARY') return h.parentElement && h.parentElement.open ? 'true' : 'false';
    const own = h.getAttribute('aria-expanded');
    if (own !== null) return own;
    const inner = h.querySelector('[aria-expanded]');
    return inner ? inner.getAttribute('aria-expanded') : null;
  };
  const isHead = el => heads.some(h => h === el || el.contains(h));
  // Element holding a (non-<summary>) header's reasoning text.
  //
  // Interim answers ("Now the Python scraper changes.") sit right after a "Reasoned for ..." section,
  // so the element following a section header may be either the section's own content panel or
  // the next interim answer. Taking the wrong one hid every interim answer (it was treated as the
  // section's content and skipped), so a candidate is only accepted as content when that is certain:
  //  - it is linked to the header (aria-controls / aria-labelledby), or
  //  - it is found next to the header without leaving the header's own wrappers, and it disappears
  //    when the section is collapsed (checked once per section by collapsing and re-expanding a
  //    finished section; text that stays visible while the section is collapsed is never content), or
  //  - for headers without an expanded state, it is a steps card (starts with a step title).
  const store = window.__roundtripSections || (window.__roundtripSections =
    {content: new WeakMap(), outside: new WeakSet()});
  const shows = el => {
    if (!el || !el.isConnected || !visible(el) || !textOf(el)) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return false;
    const r = el.getBoundingClientRect();
    return r.height > 1 && r.width > 1;
  };
  const linkedEl = h => {
    const ctrl = h.getAttribute('aria-controls') ||
      (h.querySelector('[aria-controls]') || {getAttribute: () => null}).getAttribute('aria-controls');
    if (ctrl) { const target = document.getElementById(ctrl.split(/\s+/)[0]); if (target) return target; }
    const ids = [h.id, ...Array.from(h.querySelectorAll('[id]')).map(e => e.id)].filter(Boolean);
    for (const id of ids) {
      try {
        const target = document.querySelector('[aria-labelledby~="' + CSS.escape(id) + '"]');
        if (target && target !== h && !h.contains(target) && !target.contains(h)) return target;
      } catch (_) { /* invalid id */ }
    }
    return null;
  };
  // Icons/chevrons next to a header do not end its wrapper; any other element (text, a panel) does.
  const decorative = el => !/[\p{L}\p{N}]/u.test(textOf(el)) &&
    (/^(svg|img|i|span|path|use)$/i.test(el.tagName) || !!el.querySelector('svg, img'));
  // The element following the header's own wrappers (the header plus its icons).
  const nextCandidate = h => {
    let el = h;
    for (let d = 0; d < 4 && el.parentElement && el.parentElement !== document.body; d++) {
      const parent = el.parentElement;
      if (Array.from(parent.children).some(c => c !== el && !decorative(c))) break;
      el = parent;
    }
    const next = el.nextElementSibling;
    return next && !isHead(next) && textOf(next) ? next : null;
  };
  const toggleable = h => h.tagName === 'SUMMARY' || expandedState(h) !== null;
  const firstLine = el => (proseOf(el).split('\n')[0] || '').trim();
  const stepsCard = el => {
    const line = firstLine(el);
    return stepRe.test(line) || titleRe.test(line) && line.length <= 40 && !/[.,:;!?]/.test(line);
  };
  const contentEl = h => {
    const linked = linkedEl(h);
    if (linked) return linked;
    const c = nextCandidate(h);
    if (!c || store.outside.has(c)) return null;
    if (!toggleable(h)) return stepsCard(c) ? c : null;
    if (expandedState(h) === 'false') return null;
    // Once a section's following element was seen to disappear on collapse, the section owns its
    // panel; a re-rendered panel is a new element, so any later candidate not seen outside counts.
    if (store.content.has(h)) return c;
    // A running section is the latest output: what follows it is its own (streaming) content.
    // Finished sections are verified by verifySections() before being read.
    return doneRe.test(headTitle(h)) ? null : c;
  };
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const waitFor = async (test, ms) => {
    for (let t = 0; t < ms; t += 100) { if (test()) return true; await wait(100); }
    return test();
  };
  // Text visible right after a collapsed finished section is never that section's content.
  const markOutside = () => {
    for (const h of heads) {
      if (h.tagName === 'SUMMARY' || expandedState(h) !== 'false' || !doneRe.test(headTitle(h)) || linkedEl(h)) continue;
      const c = nextCandidate(h);
      if (c && shows(c)) store.outside.add(c);
    }
  };
  // For an expanded finished section, collapse it once to see whether the following element
  // belongs to it (it disappears) or is the next interim answer (it stays), then expand it again.
  const verifySections = async () => {
    for (const h of heads) {
      if (h.tagName === 'SUMMARY' || expandedState(h) !== 'true' || !doneRe.test(headTitle(h)) || linkedEl(h)) continue;
      const c = nextCandidate(h);
      if (!c || store.outside.has(c) || store.content.has(h)) continue;
      try {
        h.click();
        const collapsed = await waitFor(() => expandedState(h) === 'false', 600);
        const hidden = collapsed && await waitFor(() => !shows(c), 1200);
        if (!collapsed || !hidden) store.outside.add(c); else store.content.set(h, true);
        if (expandedState(h) === 'false') { h.click(); await waitFor(() => expandedState(h) === 'true', 600); }
      } catch (_) { store.outside.add(c); }
    }
  };
  const contentFor = h => {
    if (h.tagName === 'SUMMARY' && h.parentElement) return proseOf(h.parentElement, h);
    return proseOf(contentEl(h));
  };
  markOutside();
  await verifySections();
  // Finished sections are collapsed again once their steps were read, as in the web UI. Copilot only
  // shows the interim answers ("Now I'll rewrite the README...") above the "Reasoned for ..." headers
  // while those sections are collapsed; keeping them expanded hid every interim answer.
  const collapseAfterRead = [];
  const blocks = heads.map((h, index) => {
    const title = headTitle(h);
    const expanded = expandedState(h);
    // Copilot collapses a block again when it finishes ("Reasoning" -> "Reasoned for 10s"):
    // expand once per title so the finished block's text stays readable.
    if (expanded === 'false' && h.dataset.roundtripExpanded !== title) {
      h.dataset.roundtripExpanded = title;
      try { h.click(); } catch (_) { /* best effort */ }
    }
    const raw = expanded === 'false' ? [] : contentFor(h).split(/\r?\n/)
      .map(l => l.trim()).filter(Boolean).map(l => l.length > lineMax ? l.slice(0, lineMax) + '\u2026' : l);
    const active = !doneRe.test(title);
    if (!active && expanded === 'true' && h.dataset.roundtripCollapsed !== title) {
      const reads = (Number(h.dataset.roundtripReads) || 0) + 1;
      h.dataset.roundtripReads = String(reads);
      // Steps already streamed are kept by the script (merge_live) while the section is collapsed.
      if (raw.length || reads >= 3) collapseAfterRead.push([h, title]);
    }
    // Split the section into steps at step-title lines ("Reasoning", "Creating file", ...).
    const steps = [];
    let step = null;
    for (const line of raw) {
      if (stepRe.test(line) || titleRe.test(line) && line.length <= 40 && !/[.,:;!?]/.test(line)) {
        step = {title: line, lines: []}; steps.push(step); continue;
      }
      if (!step) { step = {title: active && !titleRe.test(title) ? title : 'Reasoning', lines: []}; steps.push(step); }
      step.lines.push(line);
    }
    const kept = steps.filter(s => !omitRe.test(s.title))
      .map(s => ({title: s.title, total: s.lines.length, lines: s.lines.slice(-tail)})).slice(-maxSteps);
    const lines = [];
    for (const s of steps) if (!omitRe.test(s.title)) lines.push(...s.lines);
    return {index, title, active, total: lines.length, lines: lines.slice(-tail), steps: kept};
  });
  // Live intermediate answers, as in the web UI: the short interim answers Copilot writes before,
  // between and after the "Reasoned for ..." sections ("Now the Python scraper changes.",
  // "Reasoned for 5s", "Now extension.js parsing.", "Reasoned for 3s", ..., final answer).
  // Copilot may render each interim answer as its own message element, so every assistant element
  // of the current turn is read (not only the latest one), and the whole turn region around the
  // reasoning headers is read as well; whichever finds interim answers at more positions is used.
  const answers = [];
  try {
    const norm = s => String(s || '').replace(/\s+/g, ' ').trim();
    const outermost = list => list.filter(n => !list.some(o => o !== n && o.contains(n)));
    let found = [];
    for (const s of args.assistant || []) {
      try { found.push(...Array.from(document.querySelectorAll(s))); } catch (_) { /* invalid selector */ }
    }
    found = outermost(Array.from(new Set(found)).filter(n => visible(n) && textOf(n) &&
      !n.closest('[data-roundtrip-prior-msg]')))
      .sort((a, b) => a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1);
    // The current turn: the outermost ancestor of the latest reasoning header that excludes the user's prompt.
    const snippet = norm(args.prompt).slice(0, 60);
    let region = null;
    if (heads.length && snippet) {
      let el = heads[heads.length - 1].parentElement;
      while (el && el.parentElement && el.parentElement !== document.body &&
             !norm(textOf(el.parentElement)).includes(snippet)) el = el.parentElement;
      // In a continued chat the region must not reach into earlier turns.
      if (el && !norm(textOf(el)).includes(snippet) && !el.querySelector('[data-roundtrip-prior-msg]') &&
          !el.closest('[data-roundtrip-prior-msg]')) region = el;
    }
    let turnNodes = region ? found.filter(n => region.contains(n) || n.contains(region)) : [];
    if (!turnNodes.length && found.length) turnNodes = [found[found.length - 1]];
    // Reasoning headers split the text; the content of expanded sections (their steps) is skipped.
    const breaks = new Map(), skip = new Set();
    heads.forEach((h, i) => {
      breaks.set(h.tagName === 'SUMMARY' && h.parentElement ? h.parentElement : h, i);
      // Only verified section content is skipped (see contentEl), so a following interim answer
      // is never mistaken for the section's content.
      // A collapsed finished section shows no content: what is visible next to it is interim text.
      if (h.tagName !== 'SUMMARY' && !(expandedState(h) === 'false' && doneRe.test(headTitle(h)))) {
        const c = contentEl(h);
        if (c) skip.add(c);
      }
    });
    const ignored = /^(SCRIPT|STYLE|NOSCRIPT|SVG|BUTTON|TEXTAREA|INPUT|SELECT|TEMPLATE|IFRAME|CANVAS)$/;
    const collect = roots => {
      const segments = [];
      let cur = null;
      const walk = (node, root, pre) => {
        if (node.nodeType === 3) { cur.text += pre ? node.nodeValue : node.nodeValue.replace(/\s+/g, ' '); return; }
        if (node.nodeType !== 1) return;
        const el = node;
        if (el !== root && breaks.has(el)) { segments.push(cur); cur = {after: breaks.get(el) + 1, text: ''}; return; }
        if (skip.has(el) || isCode(el) || ignored.test(el.tagName.toUpperCase()) || el.getAttribute('role') === 'button' ||
            el.getAttribute('aria-hidden') === 'true') return;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || srOnly(el, cs)) return;
        if (el.tagName === 'BR') { cur.text += '\n'; return; }
        const block = !cs.display.startsWith('inline') && cs.display !== 'contents';
        const keep = pre || /^pre/.test(cs.whiteSpace);
        if (block) cur.text += '\n';
        for (const child of el.childNodes) walk(child, root, keep);
        if (block) cur.text += '\n';
      };
      for (const root of roots) {
        // Text of this root starts after every reasoning header that precedes it on screen.
        const before = heads.filter(h => !root.contains(h) &&
          (h.compareDocumentPosition(root) & Node.DOCUMENT_POSITION_FOLLOWING)).length;
        cur = {after: before, text: ''};
        walk(root, root, false);
        segments.push(cur);
      }
      const out = [];
      for (const seg of segments) {
        let text = seg.text.split('\n').map(l => l.replace(/[ \t\u00a0]+/g, ' ').trim())
          .filter(l => !/^(copilot|you)\s+said\s*:?$/i.test(l))
          // A status line rendered twice in a row ("Checking that now..." x2) is shown once.
          .filter((l, k, all) => !l || k === 0 || l !== all[k - 1]).join('\n')
          .replace(/\n{2,}/g, '\n').trim();
        if (!text) continue;
        if (text.length > args.maxChars) text = '\u2026' + text.slice(-args.maxChars);
        const last = out[out.length - 1];
        if (last && last.after === seg.after) last.text += '\n\n' + text; else out.push({after: seg.after, text});
      }
      return out;
    };
    const fromMessages = turnNodes.length ? collect(turnNodes) : [];
    const fromRegion = region ? collect([region]) : [];
    answers.push(...(fromRegion.length > fromMessages.length ? fromRegion : fromMessages));
  } catch (_) { /* live answers are best effort */ }
  for (const [h, title] of collapseAfterRead) {
    h.dataset.roundtripCollapsed = title;
    try { h.click(); } catch (_) { /* best effort */ }
  }
  // Signature of on-screen text; elapsed-time counters and animated dots are ignored.
  const root = document.querySelector('main') || document.body;
  const text = textOf(root).replace(/\d+(\.\d+)?\s*(ms|s|sec|secs|seconds?|m|min|mins|minutes?)\b/gi, '')
    .replace(/[.\u2026]+/g, '');
  let hash = 5381;
  for (let i = 0; i < text.length; i++) hash = ((hash * 33) ^ text.charCodeAt(i)) >>> 0;
  return {signature: text.length + ':' + hash, blocks, answers};
}
"""


# Marks the messages, controls and download links already on screen before a prompt is sent, so a
# continued chat never mistakes an earlier turn's reasoning, answer or download for the new one.
MARK_PRIOR_SCRIPT = r"""
(args) => {
  let n = 0;
  for (const s of args.messages) {
    try { document.querySelectorAll(s).forEach(el => { el.setAttribute('data-roundtrip-prior-msg', '1'); n++; }); }
    catch (_) { /* invalid selector */ }
  }
  document.querySelectorAll('button, a, [role="button"], summary, [aria-expanded]')
    .forEach(el => el.setAttribute('data-roundtrip-prior', '1'));
  return n;
}
"""


def mark_prior_output(page):
    "Mark earlier turns; returns the number of earlier assistant messages found (0 on failure)."
    try:
        return int(page.evaluate(MARK_PRIOR_SCRIPT, {"messages": list(ASSISTANT_SELECTORS)}) or 0)
    except Exception:
        return 0


def is_prior(node):
    "True for elements that were already on screen before this prompt was sent."
    try:
        return bool(node.evaluate("e => e.hasAttribute('data-roundtrip-prior') || "
                                  "!!e.closest('[data-roundtrip-prior-msg]')"))
    except Exception:
        return False


def read_screen(page, sel, prompt=""):
    """Return (signature, reasoning blocks, live answers); failures yield (None, None, None)."""
    try:
        data = page.evaluate(SCREEN_SCRIPT, {"tail": REASONING_TAIL_LINES, "selector": sel.get("reasoning") or "",
                                             "assistant": list(ASSISTANT_SELECTORS), "prompt": prompt,
                                             "maxChars": ANSWER_MAX_CHARS})
        return data.get("signature"), data.get("blocks") or [], data.get("answers") or []
    except Exception:
        return None, None, None


def run(cmd, cwd=None, capture=True):
    # stdin must NOT be inherited. The quit listener thread keeps a blocking readline() on our
    # stdin pipe (the VS Code extension's Cancel channel). On Windows a child process that
    # inherits a synchronous pipe handle with a pending read hangs in its C runtime start-up
    # (GetFileType on stdin blocks until that read completes), so `git rev-parse` never
    # returned and runs stalled at "Checking repository and Git working tree...".
    # GIT_TERMINAL_PROMPT=0 also prevents Git from ever waiting for interactive input.
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, env=env,
                          stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def repo_root(path: Path) -> Path:
    try:
        return Path(run(["git", "-C", str(path), "rev-parse", "--show-toplevel"]).stdout.strip()).resolve()
    except Exception as e:
        raise SystemExit(f"Not a Git worktree: {path}\n{e}")


def ensure_clean(root: Path, allow_dirty: bool):
    dirty = run(["git", "status", "--porcelain=v1"], cwd=root).stdout
    if dirty and not allow_dirty:
        raise SystemExit("Worktree is dirty. Commit/stash first, or explicitly use --allow-dirty.\n" + dirty)


def make_archive(root: Path, out_txt: Path):
    zip_path = out_txt.with_suffix(".zip")
    run(["git", "archive", "--format=zip", f"--output={zip_path}", "HEAD"], cwd=root)
    zip_path.replace(out_txt)
    with zipfile.ZipFile(out_txt) as z:
        names = z.namelist()
        if not names or any(n == ".git" or n.startswith(".git/") for n in names):
            raise SystemExit("Invalid source archive")
    return hashlib.sha256(out_txt.read_bytes()).hexdigest(), len(names)


def safe_members(z: zipfile.ZipFile):
    result = []
    for info in z.infolist():
        p = PurePosixPath(info.filename)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            raise SystemExit(f"Unsafe archive path: {info.filename}")
        if p.parts[0] == ".git":
            raise SystemExit("Returned archive contains .git")
        # Reject Unix symlinks.
        if ((info.external_attr >> 16) & 0o170000) == 0o120000:
            raise SystemExit(f"Returned archive contains symlink: {info.filename}")
        result.append(info)
    return result


def normalize_download(src: Path, dst_zip: Path):
    data = src.read_bytes()
    # Chat may return ZIP bytes with a .txt filename.
    dst_zip.write_bytes(data)
    if not zipfile.is_zipfile(dst_zip):
        raise SystemExit(f"Downloaded file is not a ZIP archive: {src.name}")
    with zipfile.ZipFile(dst_zip) as z:
        members = safe_members(z)
        if not any(not x.is_dir() for x in members):
            raise SystemExit("Downloaded ZIP contains no files")
    return hashlib.sha256(data).hexdigest()


def backup_and_replace(root: Path, result_zip: Path, backup_dir: Path):
    """Replace the entire worktree snapshot, preserving Git metadata.

    Extract and validate before touching the repository. Back up every existing
    non-.git entry (including untracked files), and restore on apply failure.
    """
    root = root.resolve()
    backup_dir = backup_dir.resolve()
    if backup_dir == root or root in backup_dir.parents:
        raise SystemExit("Backup directory must be outside the repository")
    if result_zip.resolve() == root or root in result_zip.resolve().parents:
        raise SystemExit("Result ZIP must be outside the repository")

    def copy_entry(src: Path, dst: Path):
        if src.is_symlink():
            dst.symlink_to(os.readlink(src), target_is_directory=src.is_dir())
        elif src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)

    def clear_worktree():
        for child in root.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

    with tempfile.TemporaryDirectory(prefix="copilot-extract-") as td:
        stage = Path(td)
        with zipfile.ZipFile(result_zip) as z:
            members = safe_members(z)
            z.extractall(stage, members)
        children = list(stage.iterdir())
        source = children[0] if len(children) == 1 and children[0].is_dir() else stage
        if not any(p.is_file() for p in source.rglob("*")):
            raise SystemExit("Returned repository contains no files")
        if (source / ".git").exists():
            raise SystemExit("Returned repository contains .git")

        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = backup_dir / f"{root.name}-{stamp}-{time.time_ns()}"
        backup.mkdir()
        try:
            for child in root.iterdir():
                if child.name != ".git":
                    copy_entry(child, backup / child.name)
        except Exception:
            shutil.rmtree(backup)
            raise

        try:
            clear_worktree()
            for child in source.iterdir():
                copy_entry(child, root / child.name)
        except Exception:
            clear_worktree()
            for child in backup.iterdir():
                copy_entry(child, root / child.name)
            raise
    return backup

def selectors_from_env():
    return {
        "composer": os.getenv("COPILOT_COMPOSER", 'textarea, [contenteditable="true"][role="textbox"]'),
        "file": os.getenv("COPILOT_FILE_INPUT", 'input[type="file"]'),
        "send": os.getenv("COPILOT_SEND", 'button[aria-label*="Send" i], button[data-testid*="send" i]'),
        "download": os.getenv("COPILOT_DOWNLOAD", 'a[download], a[href*="download" i], button:has-text("Download")'),
        "model_button": os.getenv("COPILOT_MODEL_BUTTON", 'button[aria-label*="model" i], button[aria-haspopup="menu"]:has-text("Auto")'),
        # Stop button shown while Copilot generates a response (clicked on Q / Cancel before quitting).
        "stop": os.getenv("COPILOT_STOP", 'button[aria-label*="Stop" i]:not([aria-label*="temporary" i]), '
                          'button[title*="Stop" i]:not([title*="temporary" i]), button[data-testid*="stop" i]'),
        "temporary_chat": os.getenv("COPILOT_TEMPORARY_CHAT", 'button[aria-label*="temporary" i], [role="button"][aria-label*="temporary" i], '
                                    'button[title*="temporary" i], button[data-testid*="temporary" i]'),
        # Optional override for reasoning headers; default detects "Reasoning"/"Reasoned for Ns" toggles.
        "reasoning": os.getenv("COPILOT_REASONING", ""),
    }


# Hard indicators: real upload progress. Soft indicators: generic spinners/progress
# test ids that Copilot may keep on screen (e.g. image thumbnail placeholders).
BUSY_STRICT = '[role="progressbar"]:visible, [aria-busy="true"]:visible'
BUSY_SOFT = '[data-testid*="progress" i]:visible, [class*="spinner" i]:visible'
THUMBNAILS = 'img[src^="blob:"]:visible, img[src^="data:"]:visible'


def _count(page, selector):
    try:
        return page.locator(selector).count()
    except Exception:
        return 0


# Attachment chips usually expose a remove/close control; a new one indicates a new chip.
CHIP_CONTROLS = ('button[aria-label*="remove" i]:visible, button[aria-label*="delete" i]:visible, '
                 '[data-testid*="attachment" i]:visible, [class*="attachment" i]:visible')
# Bounded recognition window per attempt before trying another attach method.
RECOGNIZE_SECONDS = 45


def _name_keys(filename):
    """Full name plus shorter prefixes, because chips often truncate long names."""
    stem = filename.split(".")[0]
    keys = [filename, filename[:14], stem[:10]]
    return [k for i, k in enumerate(keys) if len(k) >= 4 and k not in keys[:i]]


def _name_count(page, filename):
    """Count visible elements showing the file name as text or chip attributes (aria-label/title/alt)."""
    total = 0
    for key in _name_keys(filename):
        try:
            loc = page.get_by_text(key, exact=False)
            total += sum(1 for i in range(loc.count()) if loc.nth(i).is_visible())
        except Exception:
            pass
        quoted = key.replace("\\", "\\\\").replace('"', '\\"')
        try:
            loc = page.locator(f'[aria-label*="{quoted}" i], [title*="{quoted}" i], img[alt*="{quoted}" i]')
            total += sum(1 for i in range(loc.count()) if loc.nth(i).is_visible())
        except Exception:
            pass
    return total


def attachment_snapshot(page, filename=""):
    """Indicator counts taken before attaching, so pre-existing ones are not mistaken for this upload."""
    return {"strict": _count(page, BUSY_STRICT), "soft": _count(page, BUSY_SOFT),
            "thumbs": _count(page, THUMBNAILS), "chips": _count(page, CHIP_CONTROLS),
            "names": _name_count(page, filename) if filename else 0}


def _recognized(page, filename, baseline, image):
    """True when this file's chip is visible: its name, or (images) a new thumbnail/chip."""
    if _name_count(page, filename) > baseline.get("names", 0):
        return True
    if _count(page, CHIP_CONTROLS) > baseline.get("chips", 0):
        return True
    return image and _count(page, THUMBNAILS) > baseline.get("thumbs", 0)


def wait_for_attachment_ready(page, source_txt: Path, timeout_ms: int = 180000, require_name: bool = True,
                              baseline=None, recognize_seconds=None):
    """Wait for the current draft's attachment to finish uploading (up to 3 min).

    The attachment is recognized by a new occurrence of its file name (text or chip
    attributes), a new attachment chip, or, for images (require_name=False), a new
    thumbnail. Returns False if nothing is recognized within ``recognize_seconds`` so the
    caller can retry another attach method instead of waiting indefinitely. Upload is
    settled when progress indicators are no higher than before the attachment was added;
    persistent generic spinners are ignored after a grace period.
    """
    filename = source_txt.name
    baseline = baseline or {"strict": 0, "soft": 0, "thumbs": 0, "chips": 0, "names": 0}
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    stable_since = None
    recognized_at = None
    last_update = started
    while time.monotonic() < deadline:
        now = time.monotonic()
        if recognized_at is None:
            if _recognized(page, filename, baseline, not require_name):
                recognized_at = now
                emit_status("uploading", f"Attachment recognized: {filename}; waiting for upload to finish...")
            elif recognize_seconds is not None and now - started >= recognize_seconds:
                return False
        strict = _count(page, BUSY_STRICT)
        soft = _count(page, BUSY_SOFT)
        settled = (recognized_at is not None and strict <= baseline["strict"]
                   and (soft <= baseline["soft"] or now - recognized_at >= 30))
        if settled:
            if stable_since is None:
                stable_since = now
            if now - stable_since >= 3:
                return True
        else:
            stable_since = None
        if now - last_update >= 10:
            state = "uploading" if recognized_at is not None else "waiting for attachment chip"
            emit_status("uploading", f"Waiting for {filename} ({state}, {int(now - started)}s)...")
            last_update = now
        pause(page, 500)
    if recognized_at is None:
        return False
    raise SystemExit(f"Attachment upload did not settle within {timeout_ms // 1000}s: {filename}")


def _accept_matches(accept, path: Path):
    """Whether a file input's accept attribute allows this file (empty accept allows anything)."""
    accept = (accept or "").strip().lower()
    if not accept:
        return True
    suffix = path.suffix.lower()
    image = suffix in IMAGE_SUFFIXES
    for token in (t.strip() for t in accept.split(",")):
        if not token:
            continue
        if token in ("*", "*/*") or (token.startswith(".") and path.name.lower().endswith(token)):
            return True
        if image and token in ("image/*", "image/" + suffix.lstrip(".").replace("jpg", "jpeg")):
            return True
    return False


def _file_inputs_for(page, sel, path: Path):
    """File inputs ordered by suitability: accept matches the file type first."""
    loc = page.locator(sel["file"])
    preferred, fallback = [], []
    for i in range(_count(page, sel["file"])):
        node = loc.nth(i)
        try:
            accept = node.get_attribute("accept", timeout=2000)
        except Exception:
            accept = None
        (preferred if _accept_matches(accept, path) else fallback).append(node)
    return preferred + fallback


def _attach_with_chooser(page, path: Path):
    """Open the composer's attach/upload control and supply the file via the file chooser."""
    import re
    name = re.compile(r"attach|upload|add (a )?file|add content|add files|add image|^add$|^\+$", re.I)
    buttons = page.get_by_role("button", name=name)
    for i in range(buttons.count() - 1, -1, -1):
        button = buttons.nth(i)
        try:
            if not button.is_visible():
                continue
            with page.expect_file_chooser(timeout=8000) as fc:
                button.click(timeout=3000)
                # Some menus require choosing an upload entry after opening the "+" button.
                try:
                    page.get_by_role("menuitem", name=re.compile(r"upload|attach|file|image", re.I)).first.click(timeout=2500)
                except Exception:
                    pass
            fc.value.set_files(str(path))
            return True
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
    return False


def attach_methods(page, sel, path: Path):
    """Yield attach attempts: each matching file input, then the file-chooser button flow."""
    for node in _file_inputs_for(page, sel, path):
        def via_input(node=node):
            node.set_input_files(str(path), timeout=15000)
            return True
        yield via_input
    yield lambda: _attach_with_chooser(page, path)


def attach_file(page, sel, path: Path, require_name: bool = True):
    """Attach a file and wait until Copilot recognizes and finishes uploading it.

    Tries suitable file inputs (matching ``accept``) and then the attach button's file
    chooser; each attempt has a bounded recognition window so the run never stays
    stuck on "attaching" when an input silently rejects the file.
    """
    tried = 0
    for method in attach_methods(page, sel, path):
        baseline = attachment_snapshot(page, path.name)
        try:
            if not method():
                continue
        except Exception:
            continue
        tried += 1
        if tried > 1:
            emit_status("uploading", f"Retrying attachment of {path.name} (attempt {tried})...")
        if wait_for_attachment_ready(page, path, require_name=require_name, baseline=baseline,
                                     recognize_seconds=RECOGNIZE_SECONDS):
            return
        if tried >= 3:
            break
    raise SystemExit(f"Copilot did not accept the attachment {path.name}. Check that this file type "
                     "is supported by Copilot Chat or override COPILOT_FILE_INPUT.")


def send_with_retry(page, composer, send_selector, timeout_seconds=120):
    """Retry transiently disabled send controls, without resending an accepted prompt."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            # A cleared composer indicates that the chat accepted the submission.
            if not composer.input_value(timeout=2000).strip():
                return
        except Exception:
            try:
                if not composer.inner_text(timeout=2000).strip():
                    return
            except Exception:
                pass
        try:
            button = page.locator(send_selector).first
            raise_if_quit()
            if button.is_visible() and button.is_enabled():
                button.click(timeout=3000)
            else:
                pause(page, 1500)
                continue
        except QuitRequested:
            raise
        except Exception:
            pause(page, 1500)
            continue
        # Give the UI time to acknowledge before attempting another click.
        pause(page, 2500)
        try:
            if not composer.input_value(timeout=2000).strip():
                return
        except Exception:
            try:
                if not composer.inner_text(timeout=2000).strip():
                    return
            except Exception:
                pass
    raise SystemExit(f"Prompt was not accepted within {timeout_seconds}s after upload")

def parse_model(model: str):
    """Require an explicit provider/model pair; model names may contain punctuation."""
    if not isinstance(model, str) or model.count("/") != 1:
        raise ValueError("Model must be a provider/model pair (e.g. GPT/GPT-6)")
    provider, name = model.split("/", 1)
    if not provider.strip() or not name.strip():
        raise ValueError("Model must be a provider/model pair (e.g. GPT/GPT-6)")
    return provider, name

def select_model(page, model: str, sel):
    """Select a model through its provider submenu."""
    import re
    provider_key, requested = parse_model(model)
    button = page.locator(sel["model_button"]).first
    button.wait_for(state="visible", timeout=30000)
    button.click()
    page.wait_for_timeout(750)

    def visible_nodes(pattern):
        out = []
        loc = page.get_by_text(re.compile(pattern, re.I))
        for i in range(loc.count()):
            node = loc.nth(i)
            try:
                if node.is_visible(): out.append(node)
            except Exception: pass
        return out

    def row_for(node):
        semantic = node.locator('xpath=ancestor-or-self::*[@role="menuitem" or @role="option" or @aria-haspopup="menu" or self::button][1]')
        if semantic.count():
            try:
                if semantic.first.is_visible(): return semantic.first
            except Exception: pass
        cur = node
        for _ in range(7):
            cur = cur.locator('xpath=..')
            if not cur.count(): break
            try:
                box = cur.bounding_box()
                if box and box["width"] >= 180 and 35 <= box["height"] <= 110: return cur
            except Exception: pass
        return node

    is_claude = provider_key == "Claude"
    provider_name = "Claude/Anthropic" if is_claude else "GPT/OpenAI"
    patterns = ((r"^Claude\s*$", r"^Anthropic\s*$", r"^Claude\s+Anthropic\s*$",
                 r"^Anthropic\s+Claude\s*$") if is_claude else
                (r"^GPT\s*$", r"^OpenAI\s*$", r"^GPT\s+OpenAI\s*$"))
    provider = None
    for pattern in patterns:
        nodes = visible_nodes(pattern)
        if nodes:
            provider = row_for(nodes[-1]); break
    if provider is None:
        rows = page.locator('[role="menuitem"]:visible, [aria-haspopup="menu"]:visible')
        for i in range(rows.count()):
            row = rows.nth(i)
            try:
                text = " ".join(row.inner_text().split())
                terms = ("Claude", "Anthropic") if is_claude else ("GPT", "OpenAI")
                if all(re.search(r"\b" + term + r"\b", text, re.I) for term in terms):
                    provider = row; break
            except Exception: pass
    if provider is None:
        raise SystemExit(f"{provider_name} provider row not found after opening the model picker.")

    box = provider.bounding_box()
    if not box: raise SystemExit(f"{provider_name} provider row has no bounding box.")
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] * .25, y)
    page.wait_for_timeout(300)
    page.mouse.move(box["x"] + box["width"] - 10, y, steps=16)
    page.wait_for_timeout(1800)
    pattern = r"^" + re.escape(requested) + r"\s*$"
    options = visible_nodes(pattern)
    if not options:
        provider.click(); page.wait_for_timeout(1200); options = visible_nodes(pattern)
    if not options: raise SystemExit(f"Model {requested!r} was not found in the {provider_name} submenu.")
    option = row_for(options[-1])
    if option.get_attribute("aria-disabled") == "true": raise SystemExit(f"Model {requested!r} is disabled.")
    option.click(); page.wait_for_timeout(1000)


def temporary_chat_active(page, sel):
    """True when Copilot shows the temporary chat state (toggle pressed, heading or composer hint)."""
    import re
    try:
        toggles = page.locator(sel["temporary_chat"])
        for i in range(toggles.count()):
            node = toggles.nth(i)
            if not node.is_visible():
                continue
            for attr in ("aria-pressed", "aria-checked", "aria-selected"):
                if (node.get_attribute(attr) or "").lower() == "true":
                    return True
            label = (node.get_attribute("aria-label") or node.get_attribute("title") or "").lower()
            if re.search(r"\b(exit|leave|end|turn off|disable|stop)\b", label):
                return True
    except Exception:
        pass
    for locator in (page.get_by_text(re.compile(r"^\s*Temporary chat\s*$", re.I)),
                    page.locator('[placeholder*="Temporarily" i], [aria-placeholder*="Temporarily" i], '
                                 '[data-placeholder*="Temporarily" i], [aria-label*="Temporarily" i]')):
        try:
            if any(locator.nth(i).is_visible() for i in range(locator.count())):
                return True
        except Exception:
            pass
    return False


def enable_temporary_chat(page, sel, timeout_seconds=30):
    """Turn on Copilot's Temporary chat (top-right toggle) before model selection, without toggling it off."""
    if temporary_chat_active(page, sel):
        return
    button = page.locator(sel["temporary_chat"]).first
    try:
        button.wait_for(state="visible", timeout=timeout_seconds * 1000)
    except Exception:
        raise SystemExit("Temporary chat button was not found. Override COPILOT_TEMPORARY_CHAT, "
                         "or run with --no-temporary-chat.")
    button.click()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if temporary_chat_active(page, sel):
            page.wait_for_timeout(500)
            return
    raise SystemExit("Temporary chat could not be enabled (Copilot did not show the temporary chat state).")


# Copilot often shows a generated file only as a link or chip with its name ("repo-fixed.zip"),
# without a download attribute, "download" in the href or a "Download" button. Such links are
# also accepted when their text or href names a .zip file (never the uploaded .zip.txt archive).
ZIP_LINK_SELECTOR = ('a[href*=".zip" i], a:has-text(".zip"), [role="link"]:has-text(".zip"), '
                     'button:has-text(".zip")')
ZIP_NAME = re.compile(r"\.zip\b(?!\.txt)", re.I)
# A link that did not yield a file (e.g. it was rendered before the file was ready) is retried.
DOWNLOAD_RETRY_SECONDS = 10
DOWNLOAD_MAX_ATTEMPTS = 6


def download_candidates(page, sel, exclude_names):
    "New (this turn) download links as (node, href, key), latest first; uploaded files are skipped."
    result, keys = [], set()
    excluded = [n.lower() for n in exclude_names if n]
    for selector, need_zip in ((sel["download"], False), (ZIP_LINK_SELECTOR, True)):
        try:
            loc = page.locator(selector)
            count = loc.count()
        except Exception:
            continue
        for i in range(count - 1, -1, -1):
            node = loc.nth(i)
            if is_prior(node):
                continue  # download link of an earlier turn
            try:
                if need_zip and not node.is_visible():
                    continue
                href = node.get_attribute("href", timeout=2000) or ""
                text = node.inner_text(timeout=2000) or ""
            except Exception:
                continue
            label = (text + " " + href).lower()
            if any(n in label for n in excluded) or (need_zip and not ZIP_NAME.search(text + " " + href)):
                continue
            key = href + text
            if key in keys:
                continue
            keys.add(key)
            result.append((node, href, key))
    return result


def _zip_name(name, fallback="copilot-result.zip"):
    name = Path(str(name or "").split("?")[0].split("#")[0]).name.strip()
    return name or fallback


def click_download(ctx, page, node, work: Path, timeout_seconds=15):
    "Click a link and save the file it downloads (in this page or a popup it opens), else None."
    got, before, url = [], set(ctx.pages), page.url
    def on_download(d):
        got.append(d)
    def on_page(pg):
        pg.on("download", on_download)
    for pg in ctx.pages:
        pg.on("download", on_download)
    ctx.on("page", on_page)
    try:
        node.click(timeout=5000)
        deadline = time.monotonic() + timeout_seconds
        while not got and time.monotonic() < deadline:
            pause(page, 250)
        if not got:
            # A plain link may have navigated the chat page itself: return to the chat.
            if page.url != url:
                try:
                    page.go_back(wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
            return None
        target = work / _zip_name(got[0].suggested_filename)
        got[0].save_as(str(target))
        return target
    finally:
        try:
            ctx.remove_listener("page", on_page)
        except Exception:
            pass
        for pg in ctx.pages:
            try:
                pg.remove_listener("download", on_download)
            except Exception:
                pass
            # Close preview tabs opened by the link; the chat page stays open.
            if pg not in before and pg != page:
                try:
                    pg.close()
                except Exception:
                    pass


FETCH_BLOB_SCRIPT = r"""
async (href) => {
  const r = await fetch(href);
  if (!r.ok) return null;
  const bytes = new Uint8Array(await r.arrayBuffer());
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
"""


def fetch_download(ctx, page, href, work: Path):
    "Fetch a link's target directly (signed-in browser session); only ZIP content is accepted."
    from urllib.parse import urljoin
    if not href.startswith(("blob:", "data:")):
        href = urljoin(page.url, href)  # relative links ("/files/repo.zip")
    try:
        if href.startswith(("blob:", "data:")):
            encoded = page.evaluate(FETCH_BLOB_SCRIPT, href)
            data = base64.b64decode(encoded) if encoded else b""
        elif href.lower().startswith(("https://", "http://")):
            response = ctx.request.get(href, timeout=60000)
            data = response.body() if response.ok else b""
        else:
            return None
    except Exception:
        return None
    if not data.startswith(b"PK"):
        return None
    target = work / _zip_name(href if not href.startswith(("blob:", "data:")) else "")
    target.write_bytes(data)
    return target


def open_context(p, profile: Path, headless: bool, downloads: Path):
    return p.chromium.launch_persistent_context(
        user_data_dir=str(profile), headless=headless, accept_downloads=True,
        downloads_path=str(downloads), viewport={"width": 1440, "height": 1000},
    )


def cmd_login(args):
    from playwright.sync_api import sync_playwright
    profile = Path(args.profile).expanduser().resolve(); profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = open_context(p, profile, False, Path(tempfile.mkdtemp()))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=120000)
        print("Sign in in the opened browser. When Copilot Chat is usable, return here and press Enter.")
        input()
        ctx.close()
    print(f"Saved browser profile: {profile}")


def read_chat_response(page):
    """Read the latest assistant message, excluding the user's prompt."""
    for selector in ASSISTANT_SELECTORS:
        try:
            nodes = page.locator(selector)
            for i in range(nodes.count() - 1, -1, -1):
                node = nodes.nth(i)
                if is_prior(node):
                    continue  # earlier turn of a continued chat
                if node.is_visible():
                    text = node.inner_text(timeout=3000).strip()
                    if text:
                        return text
        except Exception:
            continue
    return ""


def cmd_fix(args):
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    parse_model(args.model)
    if getattr(args, "chat_url", None):
        validate_chat_url(args.chat_url)
        args.temporary_chat = False  # a continued chat is always a saved chat
    args.attachments = resolve_attachments(args.attach)
    if start_quit_listener():
        print("Press Q at any time to stop the Copilot response and quit.", file=sys.stderr, flush=True)
    emit_status("checking", "Checking repository and Git working tree...")
    root = repo_root(Path(args.repo).resolve())
    ensure_clean(root, args.allow_dirty)
    profile = Path(args.profile).expanduser().resolve()
    if not profile.exists():
        raise SystemExit("Browser profile missing. Run the login command first.")
    work_parent = Path(args.work_dir).expanduser().resolve() if args.work_dir else None
    if work_parent is not None:
        work_parent.mkdir(parents=True, exist_ok=True)
    # All source archives, downloads, diagnostics and rollback copies are temporary.
    # The directory is removed on success, failure, or cancellation via normal unwinding.
    emit_status("preparing", "Preparing temporary workspace...")
    with tempfile.TemporaryDirectory(prefix="copilot-roundtrip-", dir=work_parent) as temporary_work:
        return run_roundtrip(args, root, profile, Path(temporary_work), sync_playwright, PlaywrightTimeout)

def run_roundtrip(args, root, profile, work, sync_playwright, PlaywrightTimeout):
    source_txt = work / f"{root.name}-{time.strftime('%Y%m%d-%H%M%S')}.zip.txt"
    emit_status("archiving", "Creating committed repository archive...")
    source_hash, count = make_archive(root, source_txt)
    emit_status("archiving", f"Archive ready ({count} entries).")
    chat_url = getattr(args, "chat_url", None)
    continuing = bool(chat_url)
    # A saved chat is only created when Temporary chat is off; its URL is reported for follow-ups.
    track_chat = continuing or not args.temporary_chat
    prompt = (FOLLOWUP_PROMPT if continuing else BASE_PROMPT).format(request=args.request.strip())
    attachments = getattr(args, "attachments", None) or []
    if attachments:
        prompt += ATTACHMENTS_NOTE.format(names=", ".join(a.name for a in attachments))
    sel = selectors_from_env()
    downloaded = None
    chat_response = ""
    raise_if_quit()
    with sync_playwright() as p:
        emit_status("browser", "Launching Copilot browser...")
        ctx = open_context(p, profile, not args.show_browser, work)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        submitted = False
        try:
            if continuing:
                emit_status("browser", "Reopening the saved Copilot chat...")
                page.goto(chat_url, wait_until="domcontentloaded", timeout=120000)
                wait_for_ui(page)
                if conversation_url(page.url, args.url) is None:
                    raise SystemExit("The saved Copilot chat could not be reopened (Copilot showed a new chat "
                                     "instead). Start a new chat in the extension.")
                emit_status("browser", "Continuing the saved Copilot chat.")
            else:
                emit_status("browser", "Opening Copilot Chat...")
                page.goto(args.url, wait_until="domcontentloaded", timeout=120000)
                wait_for_ui(page)
            # Temporary chat must be enabled first: switching to it resets the model selection.
            # A continued chat is a saved chat, so Temporary chat is never enabled for it.
            if args.temporary_chat and not continuing:
                emit_status("temporary", "Enabling Temporary chat...")
                enable_temporary_chat(page, sel)
                emit_status("temporary", "Temporary chat enabled.")
            emit_status("model", "Checking Copilot model selection...")
            select_model(page, args.model, sel)
            emit_status("uploading", "Attaching repository archive...")
            # Do not submit until the upload belongs to this same draft message.
            attach_file(page, sel, source_txt)
            for index, extra in enumerate(attachments, 1):
                emit_status("uploading", f"Attaching additional file {index}/{len(attachments)}: {extra.name}...")
                attach_file(page, sel, extra, require_name=extra.suffix.lower() not in IMAGE_SUFFIXES)
                emit_status("uploading", f"Additional file {index}/{len(attachments)} attached: {extra.name}")
            emit_status("prompt", "Preparing modification request...")
            composer = page.locator(sel["composer"]).last
            composer.wait_for(state="visible", timeout=60000)
            composer.fill(prompt)
            # Earlier turns of a continued chat must not be read as this prompt's response.
            prior_messages = mark_prior_output(page)
            prior_keys = set()
            if continuing:
                old_links = page.locator(sel["download"])
                for i in range(old_links.count()):
                    try:
                        href = old_links.nth(i).get_attribute("href", timeout=2000) or ""
                    except Exception:
                        continue
                    if href:
                        prior_keys.add(href)
                emit_status("prompt", f"Continuing after {prior_messages} earlier Copilot message(s).")
            emit_status("prompt", "Submitting request to Copilot...")
            # From here on a response may be running: a quit must click Stop before exiting.
            submitted = True
            send_with_retry(page, composer, sel["send"])
            reported_url = None
            if continuing:
                reported_url = conversation_url(page.url, args.url) or chat_url
                emit_chat(reported_url)
            emit_status("waiting", "Waiting for Copilot to generate a downloadable repository...")
            # The timeout is an inactivity timeout: it only runs while nothing new appears on screen
            # (reasoning or response text). Any new output restarts it.
            idle_limit = args.timeout
            started = time.monotonic()
            last_activity = started
            last_signature = None
            last_live = None
            live_blocks, live_answers = [], []
            # Download attempts per link: (count, last attempt time). A link can be rendered before
            # its file is ready, so a failed attempt is retried instead of being skipped for good.
            attempts = {}
            upload_names = [source_txt.name] + [a.name for a in attachments]
            last_update = started
            while downloaded is None:
                raise_if_quit()
                now = time.monotonic()
                # A new saved chat gets its own URL once Copilot accepts the first prompt.
                if track_chat:
                    current_url = conversation_url(page.url, args.url)
                    if current_url and current_url != reported_url:
                        reported_url = current_url
                        emit_chat(current_url)
                signature, blocks, answers = read_screen(page, sel, prompt)
                if signature is not None and signature != last_signature:
                    if last_signature is not None:
                        last_activity = now
                    last_signature = signature
                # Reasoning blocks and intermediate (live) answers are streamed together.
                if blocks is not None:
                    live_blocks, live_answers = merge_live(live_blocks, live_answers, blocks, answers)
                live = None if blocks is None else (live_blocks, live_answers)
                if live is not None and live != last_live:
                    last_live = live
                    last_activity = now if (live_blocks or live_answers) else last_activity
                    emit_reasoning(live_blocks, live_answers)
                idle = now - last_activity
                if idle >= idle_limit:
                    break
                if now - last_update >= 15:
                    emit_status("waiting", f"Still waiting for Copilot response ({int(now - started)}s elapsed, "
                                           f"no new output for {int(idle)}s of {idle_limit}s allowed)...")
                    last_update = now
                now = time.monotonic()
                for node, href, key in download_candidates(page, sel, upload_names):
                    if href and href in prior_keys:
                        continue
                    count, last = attempts.get(key, (0, 0.0))
                    if count >= DOWNLOAD_MAX_ATTEMPTS or (count and now - last < DOWNLOAD_RETRY_SECONDS):
                        continue
                    attempts[key] = (count + 1, now)
                    # A link's target is fetched directly first (no navigation away from the chat);
                    # otherwise (JS-handled links and buttons) the link is clicked.
                    downloaded = fetch_download(ctx, page, href, work) if href else None
                    if downloaded is None:
                        try:
                            downloaded = click_download(ctx, page, node, work)
                        except QuitRequested:
                            raise
                        except Exception:
                            downloaded = None
                    if downloaded is not None:
                        break
                if downloaded is None:
                    pause(page, 1500)
            if downloaded is None:
                page.screenshot(path=str(work / "copilot-timeout.png"), full_page=True)
                (work / "copilot-timeout.html").write_text(page.content(), encoding="utf-8")
                ctx.close()
                raise SystemExit(f"No downloadable result: Copilot showed no new output for {args.timeout}s. Temporary diagnostics were removed with the run directory")
            # Final snapshot of the finished response, merged so nothing already shown is lost.
            # Read twice: the first read may collapse sections it just finished reading, the second
            # sees the web UI layout with the interim answers between the collapsed sections.
            for _attempt in range(2):
                page.wait_for_timeout(1000)
                _, blocks, answers = read_screen(page, sel, prompt)
                if blocks is not None:
                    final_live = merge_live(live_blocks, live_answers, blocks, answers)
                    if final_live != (live_blocks, live_answers):
                        live_blocks, live_answers = final_live
                        emit_reasoning(live_blocks, live_answers)
            if track_chat:
                current_url = conversation_url(page.url, args.url)
                if current_url and current_url != reported_url:
                    reported_url = current_url
                    emit_chat(current_url)
            chat_response = strip_speaker_label(read_chat_response(page))
            live_text = "\n\n".join(a["text"] for a in live_answers)
            if len(chat_response) < len(live_text):
                chat_response = live_text
            ctx.close()
        except QuitRequested:
            if submitted:
                emit_status("cancelled", "Quit requested: clicking Stop on the Copilot response...")
                if not stop_generation(page, sel):
                    emit_status("cancelled", "No running Copilot response was found to stop.")
            else:
                emit_status("cancelled", "Quit requested before the prompt was submitted.")
            try:
                ctx.close()
            except Exception:
                pass
            emit_status("cancelled", "Run cancelled. The repository was not changed.")
            raise SystemExit(QUIT_EXIT_CODE)
    if quit_requested():
        emit_status("cancelled", "Run cancelled before applying changes. The repository was not changed.")
        raise SystemExit(QUIT_EXIT_CODE)
    result_zip = work / f"{root.name}-modified.zip"
    emit_status("validating", "Validating downloaded ZIP contents...")
    result_hash = normalize_download(downloaded, result_zip)
    if result_hash == source_hash:
        raise SystemExit("Copilot returned a byte-identical archive; refusing to apply as no fix was detected.")
    emit_status("applying", "Backing up and applying repository changes...")
    backup = backup_and_replace(root, result_zip, work / "rollback")
    emit_status("review", "Collecting Git change summary...")
    diff = run(["git", "diff", "--stat"], cwd=root).stdout
    status = run(["git", "status", "--short"], cwd=root).stdout
    emit_status("completed", "Repository changes applied successfully.")
    print(json.dumps({"repo": str(root), "source_files": count, "result": "",
                      "backup": "", "status": status, "diff_stat": diff,
                      "chat_response": chat_response, "chat_url": reported_url or ""}, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="~/.copilot-repo-browser")
    ap.add_argument("--url", default=DEFAULT_URL)
    sp = ap.add_subparsers(dest="command", required=True)
    lp = sp.add_parser("login"); lp.set_defaults(fn=cmd_login)
    fp = sp.add_parser("fix")
    fp.add_argument("request")
    fp.add_argument("--repo", default=".")
    fp.add_argument("--work-dir", default=None, help="Optional parent for automatically deleted temporary round-trip files")
    fp.add_argument("--backup-dir", default=None, help="Deprecated; rollback copies are temporary and automatically deleted")
    fp.add_argument("--timeout", type=int, default=900,
                    help="Seconds without new on-screen output (reasoning or response) before giving up (default: 900)")
    fp.add_argument("--model", default="GPT/GPT-6",
                    help="Provider/model pair (default: GPT/GPT-6)")
    fp.add_argument("--show-browser", action="store_true", help="Show Chromium during fixes (default: invisible)")
    fp.add_argument("--headless", action="store_true", help="Deprecated; browser is already hidden by default")
    fp.add_argument("--allow-dirty", action="store_true")
    fp.add_argument("--temporary-chat", dest="temporary_chat", action="store_true", default=True,
                    help="Enable Copilot Temporary chat before selecting the model (default: enabled)")
    fp.add_argument("--no-temporary-chat", dest="temporary_chat", action="store_false",
                    help="Do not enable Temporary chat")
    fp.add_argument("--chat-url", default=None, metavar="URL",
                    help="Continue the saved Copilot chat at this URL (from a previous run's chat_url); "
                         "implies --no-temporary-chat. The current repository archive is attached again.")
    fp.add_argument("--attach", action="append", default=[], metavar="PATH",
                    help="Additional file (e.g. a picture) to attach to the prompt; repeat for several files")
    fp.set_defaults(fn=cmd_fix)
    args = ap.parse_args(); args.fn(args)

if __name__ == "__main__":
    main()
