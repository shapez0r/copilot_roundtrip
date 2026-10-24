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


def wait_for_attachment_ready(page, source_txt: Path, timeout_ms: int = 180000):
    """Wait for the attachment chip and bounded real progress indicators."""
    filename = source_txt.name
    prefix = filename[:14]
    deadline = time.time() + timeout_ms / 1000
    chip = None
    while time.time() < deadline:
        for locator in (page.get_by_text(filename, exact=False), page.get_by_text(prefix, exact=False)):
            try:
                for i in range(locator.count() - 1, -1, -1):
                    node = locator.nth(i)
                    if node.is_visible():
                        chip = node
                        break
            except Exception:
                pass
            if chip is not None:
                break
        if chip is not None:
            break
        page.wait_for_timeout(500)
    if chip is None:
        raise SystemExit(f"Attachment chip did not appear within {timeout_ms // 1000}s: {filename}")

    # This persistent banner is informational, not an upload-progress state:
    # 'Uploading from device will send a copy to OneDrive (work/school).'
    settle_deadline = time.time() + 12
    while time.time() < settle_deadline:
        busy = page.locator(
            '[role="progressbar"]:visible, [aria-busy="true"]:visible, '
            '[data-testid*="progress" i]:visible, [class*="spinner" i]:visible'
        )
        if busy.count() == 0:
            page.wait_for_timeout(2000)
            return
        page.wait_for_timeout(500)
    page.wait_for_timeout(1000)


def select_model(page, model: str, sel):
    """Select a model through the two-level GPT/OpenAI provider menu."""
    import re
    requested = model.strip()
    if requested.lower() == "auto":
        return
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

    provider = None
    for pattern in (r"^GPT\s*$", r"^OpenAI\s*$", r"^GPT\s+OpenAI\s*$"):
        nodes = visible_nodes(pattern)
        if nodes:
            provider = row_for(nodes[-1]); break
    if provider is None:
        rows = page.locator('[role="menuitem"]:visible, [aria-haspopup="menu"]:visible')
        for i in range(rows.count()):
            row = rows.nth(i)
            try:
                text = " ".join(row.inner_text().split())
                if re.search(r"\bGPT\b", text, re.I) and re.search(r"\bOpenAI\b", text, re.I):
                    provider = row; break
            except Exception: pass
    if provider is None:
        raise SystemExit("GPT/OpenAI provider row not found after opening the model picker.")

    box = provider.bounding_box()
    if not box: raise SystemExit("GPT/OpenAI provider row has no bounding box.")
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] * .25, y)
    page.wait_for_timeout(300)
    page.mouse.move(box["x"] + box["width"] - 10, y, steps=16)
    page.wait_for_timeout(1800)
    pattern = r"^" + re.escape(requested) + r"\s*$"
    options = visible_nodes(pattern)
    if not options:
        provider.click(); page.wait_for_timeout(1200); options = visible_nodes(pattern)
    if not options: raise SystemExit(f"Model {requested!r} was not found in the GPT submenu.")
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
    root = repo_root(Path(args.repo).resolve())
    ensure_clean(root, args.allow_dirty)
    profile = Path(args.profile).expanduser().resolve()
    if not profile.exists():
        raise SystemExit("Browser profile missing. Run the login command first.")
    work = Path(args.work_dir).expanduser().resolve(); work.mkdir(parents=True, exist_ok=True)
    source_txt = work / f"{root.name}-{int(time.time())}.zip.txt"
    source_hash, count = make_archive(root, source_txt)
    prompt = BASE_PROMPT.format(request=args.request.strip())
    sel = selectors_from_env()
    downloaded = None
    chat_response = ""
    with sync_playwright() as p:
        ctx = open_context(p, profile, args.headless, work)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=120000)
        select_model(page, args.model, sel)
        # Prefer direct file input; fall back to file chooser triggered by Add/Attach button.
        file_input = page.locator(sel["file"]).first
        try:
            file_input.set_input_files(str(source_txt), timeout=15000)
        except Exception:
            with page.expect_file_chooser(timeout=30000) as fc:
                page.get_by_role("button", name=lambda n: bool(n) and ("attach" in n.lower() or "add" in n.lower())).first.click()
            fc.value.set_files(str(source_txt))
        # Do not submit until the upload belongs to this same draft message.
        wait_for_attachment_ready(page, source_txt)
        composer = page.locator(sel["composer"]).last
        composer.wait_for(state="visible", timeout=60000)
        composer.fill(prompt)
        try:
            page.locator(sel["send"]).first.click(timeout=15000)
        except Exception:
            composer.press("Enter")
        deadline = time.time() + args.timeout
        seen = set()
        while time.time() < deadline and downloaded is None:
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
            raise SystemExit(f"No downloadable result within {args.timeout}s. Diagnostics saved in {work}")
        chat_response = read_chat_response(page)
        ctx.close()
    result_zip = work / f"{root.name}-modified.zip"
    result_hash = normalize_download(downloaded, result_zip)
    if result_hash == source_hash:
        raise SystemExit("Copilot returned a byte-identical archive; refusing to apply as no fix was detected.")
    backup = backup_and_replace(root, result_zip, Path(args.backup_dir).expanduser().resolve())
    diff = run(["git", "diff", "--stat"], cwd=root).stdout
    status = run(["git", "status", "--short"], cwd=root).stdout
    print(json.dumps({"repo": str(root), "source_files": count, "result": str(result_zip),
                      "backup": str(backup), "status": status, "diff_stat": diff,
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
    fp.add_argument("--work-dir", default="~/.cache/copilot-repo-roundtrip")
    fp.add_argument("--backup-dir", default="~/.local/share/copilot-repo-backups")
    fp.add_argument("--timeout", type=int, default=900)
    fp.add_argument("--model", default="auto", help="Exact visible model label, e.g. auto or GPT-6")
    fp.add_argument("--headless", action="store_true")
    fp.add_argument("--allow-dirty", action="store_true")
    fp.set_defaults(fn=cmd_fix)
    args = ap.parse_args(); args.fn(args)

if __name__ == "__main__":
    main()
