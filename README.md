# Copilot Repository Round-Trip

Experimental Python/Playwright automation for submitting a local Git repository to Microsoft 365 Copilot Chat for a requested change, downloading the resulting archive, and applying it to the working tree.

> **Experimental:** This relies on browser UI selectors that can change. Inspect all generated changes before committing. The script does not commit or push.

## Contents

- `vscode-extension/scripts/copilot_repo_roundtrip.py` — command-line tool (`login` and `fix`).

## Requirements

- Python 3.9+ (the script uses modern type annotations).
- Git installed and available on `PATH`.
- A Microsoft 365 account with access to Copilot Chat.
- Playwright for Python and its Chromium browser.
- A local Git repository with a valid `HEAD` commit.

## Install (Windows PowerShell)

From the folder containing `vscode-extension/scripts/copilot_repo_roundtrip.py`:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install playwright
python -m playwright install chromium
```

If PowerShell blocks activation, you can run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` and retry activation. The archive does **not** include a requirements file.

## Sign in

```powershell
python .\vscode-extension/scripts/copilot_repo_roundtrip.py login
```

A Chromium window opens at `https://m365.cloud.microsoft/chat/`. Sign in, navigate to a usable Copilot Chat page, then return to the terminal and press Enter. The persistent browser profile defaults to `~/.copilot-repo-browser`; **do not share or commit it**, as it can contain authentication state.

## Request a fix

```powershell
python .\vscode-extension/scripts/copilot_repo_roundtrip.py fix "Add a proper README.md" --repo "C:\path\to\repository"
```

To request a specific model (its label must match the visible UI):

```powershell
python .\vscode-extension/scripts/copilot_repo_roundtrip.py fix "Add a proper README.md" --repo "C:\path\to\repository" --model "GPT/GPT-6"
```

`--model` accepts a provider/model pair such as `Claude/Opus 5.5` or `GPT/GPT-6` (the default). The slash separates provider and model; spaces, dots, and dashes in the model name are preserved. The chosen model must be available in the account's model menu; otherwise selection fails. Menu layout and model availability may change.

The tool requires a clean working tree by default. `--allow-dirty` bypasses this check, but the uploaded snapshot is still made from `HEAD`, **not** from uncommitted or untracked changes.

## How it works

1. Confirms the target is a Git worktree and checks cleanliness unless `--allow-dirty` is set.
2. Runs `git archive --format=zip HEAD`, saving ZIP bytes under a `.zip.txt` name for attachment.
3. Opens Copilot Chat, optionally selects the model, uploads the archive, waits for an attachment chip and bounded progress checks, then submits the repair request.
4. Waits for a downloadable response; if none appears before the timeout, saves a screenshot and HTML diagnostics.
5. Checks that the download is a ZIP with files, rejects traversal paths, `.git` entries and Unix symlinks, and refuses a byte-identical archive.
6. Backs up all existing worktree files (including untracked files), clears the worktree except `.git`, then copies the complete extracted repository into place. On an apply failure, it restores the backup. It prints Git status, diff statistics, and the latest Copilot assistant chat response (when available) in the JSON output.

**Important:** Files omitted from the returned ZIP are deleted from the worktree; `.git` is preserved. Untracked files are also removed from the worktree (but included in the backup). Rollback backups and downloads are temporary and kept outside the repository. It may add files from the response. It does not independently verify that Copilot changed only what was requested. Review the diff and new files carefully. Files excluded by `git archive HEAD` are not sent to Copilot.

## Review the result

From the target repository:

```powershell
git status
git diff
git diff --check
```

Backups default to `~/.local/share/copilot-repo-backups`; working downloads and diagnostics default to `~/.cache/copilot-repo-roundtrip`. These paths can be changed with command-line options.

## Command-line options

```text
python vscode-extension/scripts/copilot_repo_roundtrip.py [--profile PATH] [--url URL] login
python vscode-extension/scripts/copilot_repo_roundtrip.py [--profile PATH] [--url URL] fix REQUEST [options]
```

For `fix`, options include:

- `--repo PATH` — target Git repository (default: current directory).
- `--model LABEL` — provider/model pair: `Claude/Opus 5.5` or `GPT/GPT-6` (default: `GPT/GPT-6`).
- `--work-dir PATH` — temporary archives, downloads and diagnostics.
- `--backup-dir PATH` — location for timestamped complete worktree backups.
- `--timeout SECONDS` — download wait timeout (default: `900`).
- `--headless` — run Chromium without a visible window (requires an existing signed-in profile).
- `--allow-dirty` — allow a dirty worktree (use with caution).

Use `python vscode-extension/scripts/copilot_repo_roundtrip.py --help` and `python vscode-extension/scripts/copilot_repo_roundtrip.py fix --help` for the CLI help.

## UI selector overrides

If Copilot's interface changes, you can override its Playwright selectors via environment variables:

```powershell
$env:COPILOT_COMPOSER='textarea'
$env:COPILOT_FILE_INPUT='input[type="file"]'
$env:COPILOT_SEND='button[aria-label*="Send" i]'
$env:COPILOT_DOWNLOAD='a[download]'
$env:COPILOT_MODEL_BUTTON='button[aria-label*="model" i]'
```

The informational OneDrive upload notice is **not** treated as an upload-progress indicator. Upload readiness is determined from the attachment chip and actual progress elements, with a bounded wait.

## Security notes

- Only use this with repositories whose contents are approved for upload to your organization's Copilot environment.
- Keep the browser profile, downloaded archives and backup directories private.
- Validate the resulting source changes and run your project's tests before committing.
- Browser automation and the download flow are dependent on the current Copilot UI and are not guaranteed to work across updates.

hello

hello2

hello3

hello4

hello 5

hello 6

Round-trip upload names use local timestamps such as `repo-20261024-191212.zip.txt`. Temporary source ZIPs, downloaded files and rollback backups are deleted when the run finishes (including errors). The applied repository changes remain in the working tree.

The Python automation has one canonical copy at `vscode-extension/scripts/copilot_repo_roundtrip.py`; the VS Code extension uses this same file. Fix runs are headless by default. Upload readiness is monitored for up to 180 seconds, and prompt submission retries for up to 120 seconds.

hello 9
hello 10
hello 11
hello 15
hello 16
hello 20
Last updated by AI model: Claude Opus 5.5 (Microsoft Copilot) — 2026-10-24 21:53:30 UTC
