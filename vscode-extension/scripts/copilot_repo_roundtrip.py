#!/usr/bin/env python3
"""Experimental M365 Copilot Chat browser automation for a local Git worktree.

It archives the Git index (therefore excludes .git and untracked files), uploads the
ZIP bytes under a .txt filename, sends a repair prompt, waits for a downloaded file,
validates the returned archive, and replaces the worktree after a backup.

UI automation is unsupported and selectors can change. Run `login` once, then `fix`.
"""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess, sys, tempfile, time, zipfile
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

def run(cmd, cwd=None, capture=True):
    return subprocess.run(cmd, cwd=cwd, check=True, text=True,
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


def attachment_snapshot(page):
    """Indicator counts taken before attaching, so pre-existing ones are not mistaken for this upload."""
    return {"strict": _count(page, BUSY_STRICT), "soft": _count(page, BUSY_SOFT),
            "thumbs": _count(page, THUMBNAILS)}


def _name_visible(page, filename):
    """Match the file name as visible text or in chip attributes (aria-label/title/alt)."""
    keys = [filename, filename[:14]]
    for key in keys:
        try:
            loc = page.get_by_text(key, exact=False)
            if any(loc.nth(i).is_visible() for i in range(loc.count())):
                return True
        except Exception:
            pass
        quoted = key.replace("\\", "\\\\").replace('"', '\\"')
        try:
            loc = page.locator(f'[aria-label*="{quoted}" i], [title*="{quoted}" i], img[alt*="{quoted}" i]')
            if any(loc.nth(i).is_visible() for i in range(loc.count())):
                return True
        except Exception:
            pass
    return False


def wait_for_attachment_ready(page, source_txt: Path, timeout_ms: int = 180000, require_name: bool = True,
                              baseline=None):
    """Wait for the current draft's attachment to finish uploading (up to 3 min).

    The attachment is recognized by its file name (text or chip attributes) or, for
    images (require_name=False), by a new thumbnail or a short grace period. Upload is
    settled when progress indicators are no higher than before the attachment was added;
    persistent generic spinners on an already recognized image are ignored after a grace period.
    """
    filename = source_txt.name
    baseline = baseline or {"strict": 0, "soft": 0, "thumbs": 0}
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    stable_since = None
    recognized_at = None
    last_update = started
    while time.monotonic() < deadline:
        now = time.monotonic()
        if recognized_at is None:
            if _name_visible(page, filename):
                recognized_at = now
            elif not require_name and (_count(page, THUMBNAILS) > baseline["thumbs"] or now - started >= 10):
                recognized_at = now
        strict = _count(page, BUSY_STRICT)
        soft = _count(page, BUSY_SOFT)
        settled = (recognized_at is not None and strict <= baseline["strict"]
                   and (soft <= baseline["soft"] or now - recognized_at >= 30))
        if settled:
            if stable_since is None:
                stable_since = now
            if now - stable_since >= 5:
                return
        else:
            stable_since = None
        if now - last_update >= 15:
            state = "uploading" if recognized_at is not None else "waiting for attachment chip"
            emit_status("uploading", f"Waiting for {filename} ({state}, {int(now - started)}s)...")
            last_update = now
        page.wait_for_timeout(500)
    raise SystemExit(f"Attachment upload did not settle within {timeout_ms // 1000}s: {filename}")


def attach_file(page, sel, path: Path):
    """Prefer direct file input; fall back to file chooser triggered by Add/Attach button."""
    file_input = page.locator(sel["file"]).first
    try:
        file_input.set_input_files(str(path), timeout=15000)
    except Exception:
        with page.expect_file_chooser(timeout=30000) as fc:
            page.get_by_role("button", name=lambda n: bool(n) and ("attach" in n.lower() or "add" in n.lower())).first.click()
        fc.value.set_files(str(path))


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
            if button.is_visible() and button.is_enabled():
                button.click(timeout=3000)
            else:
                page.wait_for_timeout(1500)
                continue
        except Exception:
            page.wait_for_timeout(1500)
            continue
        # Give the UI time to acknowledge before attempting another click.
        page.wait_for_timeout(2500)
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
    selectors = (
        '[data-message-author-role="assistant"]',
        '[data-role="assistant"]',
        '[data-testid*="assistant-message" i]',
        '[data-testid*="copilot-message" i]',
        '[class*="assistant-message" i]',
    )
    for selector in selectors:
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
    with sync_playwright() as p:
        emit_status("browser", "Launching Copilot browser...")
        ctx = open_context(p, profile, not args.show_browser, work)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        emit_status("browser", "Opening Copilot Chat...")
        page.goto(args.url, wait_until="domcontentloaded", timeout=120000)
        emit_status("model", "Checking Copilot model selection...")
        select_model(page, args.model, sel)
        emit_status("uploading", "Attaching repository archive...")
        baseline = attachment_snapshot(page)
        attach_file(page, sel, source_txt)
        # Do not submit until the upload belongs to this same draft message.
        emit_status("uploading", "Waiting for attachment upload to finish...")
        wait_for_attachment_ready(page, source_txt, baseline=baseline)
        for index, extra in enumerate(attachments, 1):
            emit_status("uploading", f"Attaching additional file {index}/{len(attachments)}: {extra.name}...")
            baseline = attachment_snapshot(page)
            attach_file(page, sel, extra)
            wait_for_attachment_ready(page, extra, require_name=extra.suffix.lower() not in IMAGE_SUFFIXES,
                                      baseline=baseline)
            emit_status("uploading", f"Additional file {index}/{len(attachments)} attached: {extra.name}")
        emit_status("prompt", "Preparing modification request...")
        composer = page.locator(sel["composer"]).last
        composer.wait_for(state="visible", timeout=60000)
        composer.fill(prompt)
        emit_status("prompt", "Submitting request to Copilot...")
        send_with_retry(page, composer, sel["send"])
        emit_status("waiting", "Waiting for Copilot to generate a downloadable repository...")
        deadline = time.time() + args.timeout
        seen = set()
        last_update = time.monotonic()
        while time.time() < deadline and downloaded is None:
            if time.monotonic() - last_update >= 15:
                emit_status("waiting", f"Still waiting for Copilot response ({int(args.timeout - max(0, deadline - time.time()))}s elapsed)...")
                last_update = time.monotonic()
            candidates = page.locator(sel["download"])
            for i in range(candidates.count() - 1, -1, -1):
                node = candidates.nth(i)
                key = (node.get_attribute("href") or "") + (node.inner_text(timeout=2000) or "")
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
                page.wait_for_timeout(3000)
        if downloaded is None:
            page.screenshot(path=str(work / "copilot-timeout.png"), full_page=True)
            (work / "copilot-timeout.html").write_text(page.content(), encoding="utf-8")
            ctx.close()
            raise SystemExit(f"No downloadable result within {args.timeout}s. Temporary diagnostics were removed with the run directory")
        chat_response = read_chat_response(page)
        ctx.close()
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
    fp.add_argument("--timeout", type=int, default=900)
    fp.add_argument("--model", default="GPT/GPT-6",
                    help="Provider/model pair (default: GPT/GPT-6)")
    fp.add_argument("--show-browser", action="store_true", help="Show Chromium during fixes (default: invisible)")
    fp.add_argument("--headless", action="store_true", help="Deprecated; browser is already hidden by default")
    fp.add_argument("--allow-dirty", action="store_true")
    fp.add_argument("--attach", action="append", default=[], metavar="PATH",
                    help="Additional file (e.g. a picture) to attach to the prompt; repeat for several files")
    fp.set_defaults(fn=cmd_fix)
    args = ap.parse_args(); args.fn(args)

if __name__ == "__main__":
    main()
