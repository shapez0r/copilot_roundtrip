#!/usr/bin/env python3
"""Experimental M365 Copilot Chat browser automation for a local Git worktree.

It archives the Git index (therefore excludes .git and untracked files), uploads the
ZIP bytes under a .txt filename, sends a repair prompt, waits for a downloaded file,
validates the returned archive, and replaces the worktree after a backup.

UI automation is unsupported and selectors can change. Run `login` once, then `fix`.
"""
from __future__ import annotations
import argparse, atexit, hashlib, json, os, re, shutil, subprocess, sys, tempfile, threading, time, zipfile
from pathlib import Path, PurePosixPath

DEFAULT_URL = "https://m365.cloud.microsoft/chat/"
BASE_PROMPT = (
    "Here is my repository archive. The attached .txt file contains ZIP bytes. "
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
        merged_blocks.append(block)
    # Blocks that temporarily vanished from the screen are kept as well.
    for old in prev_blocks[len(merged_blocks):]:
        merged_blocks.append(dict(old, active=False))
    texts = {}
    for answer in prev_answers or []:
        texts[answer["after"]] = answer["text"]
    for answer in answers or []:
        text = strip_speaker_label(answer.get("text"))
        if not text:
            continue
        after = answer.get("after", 0)
        if len(text) >= len(texts.get(after, "")):
            texts[after] = text
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
# and (2) the reasoning blocks ("Reasoning...", "Reasoned for 38s", ...). Collapsed reasoning
# blocks are expanded once so their text can be read in both visible and hidden browser modes.
# (3) The live intermediate answers: the latest assistant message's text split at those blocks.
# Code blocks (e.g. the code shown under "Coding and executing") are left out of both the
# reasoning lines and the live answers; only the step titles and prose are streamed.
SCREEN_SCRIPT = r"""
(args) => {
  const tail = args.tail, custom = args.selector;
  const titleRe = /^\s*(reasoning|reasoned|thinking|thought|thought for)\b/i;
  const visible = el => !!el && el.getClientRects().length > 0;
  const textOf = el => (el && (el.innerText || el.textContent) || '').trim();
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
  const hasCode = el => {
    if (!el || el.nodeType !== 1) return false;
    try { if (el.querySelector(codeSel + ', code')) return Array.from(el.querySelectorAll(codeSel + ', code')).some(isCode); }
    catch (_) { /* fall through */ }
    return false;
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
      if (cs.display === 'none' || cs.visibility === 'hidden') return;
      if (node.tagName === 'BR') { out += '\n'; return; }
      const block = !cs.display.startsWith('inline') && cs.display !== 'contents';
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
  heads = heads.filter(h => {
    const t = textOf(h);
    return visible(h) && t && t.length <= 120 && (customHeads || titleRe.test(t));
  });
  // Keep the outermost header when several nested elements match the same title.
  heads = heads.filter(h => !heads.some(o => o !== h && o.contains(h)));
  const expandedState = h => {
    if (h.tagName === 'SUMMARY') return h.parentElement && h.parentElement.open ? 'true' : 'false';
    const own = h.getAttribute('aria-expanded');
    if (own !== null) return own;
    const inner = h.querySelector('[aria-expanded]');
    return inner ? inner.getAttribute('aria-expanded') : null;
  };
  const isHead = el => heads.some(h => h === el || el.contains(h));
  // Element holding a (non-<summary>) header's reasoning text.
  const contentEl = h => {
    const ctrl = h.getAttribute('aria-controls') ||
      (h.querySelector('[aria-controls]') || {getAttribute: () => null}).getAttribute('aria-controls');
    if (ctrl) { const target = document.getElementById(ctrl); if (target) return target; }
    let el = h;
    for (let depth = 0; depth < 4 && el; depth++) {
      const next = el.nextElementSibling;
      if (next && !isHead(next) && textOf(next)) return next;
      el = el.parentElement;
    }
    return null;
  };
  const contentFor = h => {
    if (h.tagName === 'SUMMARY' && h.parentElement) {
      if (hasCode(h.parentElement)) return proseOf(h.parentElement, h);
      const full = textOf(h.parentElement), title = textOf(h);
      return full.startsWith(title) ? full.slice(title.length) : full;
    }
    const content = contentEl(h);
    return hasCode(content) ? proseOf(content) : textOf(content);
  };
  const blocks = heads.map((h, index) => {
    const title = textOf(h).split('\n')[0].replace(/[\s>\u203a\u00bb]+$/, '').trim();
    const expanded = expandedState(h);
    // Copilot collapses a block again when it finishes ("Reasoning" -> "Reasoned for 10s"):
    // expand once per title so the finished block's text stays readable.
    if (expanded === 'false' && h.dataset.roundtripExpanded !== title) {
      h.dataset.roundtripExpanded = title;
      try { h.click(); } catch (_) { /* best effort */ }
    }
    const lines = expanded === 'false' ? [] : contentFor(h).split(/\r?\n/)
      .map(l => l.trim()).filter(Boolean).map(l => l.length > 500 ? l.slice(0, 500) + '\u2026' : l);
    const active = !/^\s*(reasoned|thought)\b/i.test(title);
    return {index, title, active, total: lines.length, lines: lines.slice(-tail)};
  });
  // Live intermediate answers: the latest assistant message's visible text, split at the
  // reasoning headers ("answer", "Reasoned for 38s", "answer", "Reasoned for 4s", "answer").
  const answers = [];
  try {
    const norm = s => String(s || '').replace(/\s+/g, ' ').trim();
    let root = null;
    for (const s of args.assistant || []) {
      let nodes = [];
      try { nodes = Array.from(document.querySelectorAll(s)); } catch (_) { nodes = []; }
      for (let i = nodes.length - 1; i >= 0 && !root; i--) if (visible(nodes[i]) && textOf(nodes[i])) root = nodes[i];
      if (root) break;
    }
    // Fallback: the outermost ancestor of the latest reasoning header that excludes the user's prompt.
    const snippet = norm(args.prompt).slice(0, 60);
    if (!root && heads.length && snippet) {
      let el = heads[heads.length - 1].parentElement;
      while (el && el.parentElement && el.parentElement !== document.body &&
             !norm(textOf(el.parentElement)).includes(snippet)) el = el.parentElement;
      if (el && !norm(textOf(el)).includes(snippet)) root = el;
    }
    if (root) {
      // Reasoning headers split the text; their expanded reasoning content is skipped.
      const breaks = new Map(), skip = new Set();
      heads.forEach((h, i) => {
        breaks.set(h.tagName === 'SUMMARY' && h.parentElement ? h.parentElement : h, i);
        if (h.tagName !== 'SUMMARY' && expandedState(h) !== 'false') {
          const c = contentEl(h);
          if (c && !c.contains(root)) skip.add(c);
        }
      });
      const before = heads.filter(h => !root.contains(h) &&
        (h.compareDocumentPosition(root) & Node.DOCUMENT_POSITION_FOLLOWING)).length;
      const segments = [];
      let cur = {after: before, text: ''};
      const ignored = /^(SCRIPT|STYLE|NOSCRIPT|SVG|BUTTON|TEXTAREA|INPUT|SELECT|TEMPLATE|IFRAME|CANVAS)$/;
      const walk = (node, pre) => {
        if (node.nodeType === 3) { cur.text += pre ? node.nodeValue : node.nodeValue.replace(/\s+/g, ' '); return; }
        if (node.nodeType !== 1) return;
        const el = node;
        if (el !== root && breaks.has(el)) { segments.push(cur); cur = {after: breaks.get(el) + 1, text: ''}; return; }
        if (skip.has(el) || isCode(el) || ignored.test(el.tagName.toUpperCase()) || el.getAttribute('role') === 'button' ||
            el.getAttribute('aria-hidden') === 'true') return;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return;
        if (el.tagName === 'BR') { cur.text += '\n'; return; }
        const block = !cs.display.startsWith('inline') && cs.display !== 'contents';
        const keep = pre || /^pre/.test(cs.whiteSpace);
        if (block) cur.text += '\n';
        for (const child of el.childNodes) walk(child, keep);
        if (block) cur.text += '\n';
      };
      walk(root, false);
      segments.push(cur);
      for (const seg of segments) {
        let text = seg.text.split('\n').map(l => l.replace(/[ \t\u00a0]+/g, ' ').trim()).join('\n')
          .replace(/\n{2,}/g, '\n').trim();
        if (!text) continue;
        if (text.length > args.maxChars) text = '\u2026' + text.slice(-args.maxChars);
        const last = answers[answers.length - 1];
        if (last && last.after === seg.after) last.text += '\n\n' + text; else answers.push({after: seg.after, text});
      }
    }
  } catch (_) { /* live answers are best effort */ }
  // Signature of on-screen text; elapsed-time counters and animated dots are ignored.
  const root = document.querySelector('main') || document.body;
  const text = textOf(root).replace(/\d+(\.\d+)?\s*(ms|s|sec|secs|seconds?|m|min|mins|minutes?)\b/gi, '')
    .replace(/[.\u2026]+/g, '');
  let hash = 5381;
  for (let i = 0; i < text.length; i++) hash = ((hash * 33) ^ text.charCodeAt(i)) >>> 0;
  return {signature: text.length + ':' + hash, blocks, answers};
}
"""


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
    prompt = BASE_PROMPT.format(request=args.request.strip())
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
            emit_status("browser", "Opening Copilot Chat...")
            page.goto(args.url, wait_until="domcontentloaded", timeout=120000)
            wait_for_ui(page)
            # Temporary chat must be enabled first: switching to it resets the model selection.
            if args.temporary_chat:
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
            emit_status("prompt", "Submitting request to Copilot...")
            # From here on a response may be running: a quit must click Stop before exiting.
            submitted = True
            send_with_retry(page, composer, sel["send"])
            emit_status("waiting", "Waiting for Copilot to generate a downloadable repository...")
            # The timeout is an inactivity timeout: it only runs while nothing new appears on screen
            # (reasoning or response text). Any new output restarts it.
            idle_limit = args.timeout
            started = time.monotonic()
            last_activity = started
            last_signature = None
            last_live = None
            live_blocks, live_answers = [], []
            seen = set()
            last_update = started
            while downloaded is None:
                raise_if_quit()
                now = time.monotonic()
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
                candidates = page.locator(sel["download"])
                for i in range(candidates.count() - 1, -1, -1):
                    node = candidates.nth(i)
                    try:
                        key = (node.get_attribute("href") or "") + (node.inner_text(timeout=2000) or "")
                    except Exception:
                        continue
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        with page.expect_download(timeout=15000) as di:
                            node.click()
                        d = di.value
                        downloaded = work / d.suggested_filename
                        d.save_as(str(downloaded))
                        break
                    except PlaywrightTimeout:
                        continue
                    except Exception:
                        continue
                if downloaded is None:
                    pause(page, 1500)
            if downloaded is None:
                page.screenshot(path=str(work / "copilot-timeout.png"), full_page=True)
                (work / "copilot-timeout.html").write_text(page.content(), encoding="utf-8")
                ctx.close()
                raise SystemExit(f"No downloadable result: Copilot showed no new output for {args.timeout}s. Temporary diagnostics were removed with the run directory")
            # Final snapshot of the finished response, merged so nothing already shown is lost.
            page.wait_for_timeout(1000)
            _, blocks, answers = read_screen(page, sel, prompt)
            if blocks is not None:
                final_live = merge_live(live_blocks, live_answers, blocks, answers)
                if final_live != (live_blocks, live_answers):
                    live_blocks, live_answers = final_live
                    emit_reasoning(live_blocks, live_answers)
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
                      "chat_response": chat_response}, indent=2))


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
    fp.add_argument("--attach", action="append", default=[], metavar="PATH",
                    help="Additional file (e.g. a picture) to attach to the prompt; repeat for several files")
    fp.set_defaults(fn=cmd_fix)
    args = ap.parse_args(); args.fn(args)

if __name__ == "__main__":
    main()
