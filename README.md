# Copilot Repository Round-Trip

Experimental Python/Playwright automation for submitting a local Git repository to Microsoft 365 Copilot Chat for a requested change, downloading the resulting archive, and applying it to the working tree.

> **Experimental:** This relies on browser UI selectors that can change. Inspect all generated changes before committing. The script does not commit or push.

## Contents

- `vscode-extension/scripts/copilot_repo_roundtrip.py` — command-line tool (`login` and `fix`). This is the single canonical copy of the automation; the VS Code extension in `vscode-extension/` uses the same file (see `vscode-extension/README.md`).

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
python .\vscode-extension\scripts\copilot_repo_roundtrip.py login
```

A Chromium window opens at `https://m365.cloud.microsoft/chat/`. Sign in, navigate to a usable Copilot Chat page, then return to the terminal and press Enter. The persistent browser profile defaults to `~/.copilot-repo-browser`; **do not share or commit it**, as it can contain authentication state.

## Request a fix

```powershell
python .\vscode-extension\scripts\copilot_repo_roundtrip.py fix "Add a proper README.md" --repo "C:\path\to\repository"
```

To request a specific model (its label must match the visible UI):

```powershell
python .\vscode-extension\scripts\copilot_repo_roundtrip.py fix "Add a proper README.md" --repo "C:\path\to\repository" --model "GPT/GPT-6"
```

`--model` accepts a provider/model pair such as `Claude/Opus 5.5` or `GPT/GPT-6` (the default). The slash separates provider and model; spaces, dots, and dashes in the model name are preserved. The chosen model must be available in the account's model menu; otherwise selection fails. Menu layout and model availability may change.

To attach additional files to the prompt (for example a screenshot, picture, or specification), pass a path to each file with `--attach` (repeatable):

```powershell
python .\vscode-extension\scripts\copilot_repo_roundtrip.py fix "Match the layout in the screenshot" --repo "C:\path\to\repository" --attach "C:\pictures\layout.png" --attach "C:\docs\spec.pdf"
```

Attached files are uploaded after the repository archive and mentioned in the prompt as context; they are not added to the repository.

To continue a saved (non-temporary) Copilot chat, pass the `chat_url` printed by an earlier run with `--no-temporary-chat`:

```powershell
python .\vscode-extension\scripts\copilot_repo_roundtrip.py fix "Also update the tests" --repo "C:\path\to\repository" --chat-url "https://m365.cloud.microsoft/chat/..."
```

The follow-up is sent in that same Copilot chat. Because Copilot's sandbox is erased between prompts, the current committed repository state is archived and attached again for every prompt, and the follow-up prompt tells Copilot that this archive supersedes earlier ones. Reasoning, answers and download links of earlier turns are ignored, so only the new response's ZIP is applied. If the saved chat cannot be reopened, the run fails instead of starting a different chat.

While a fix runs, press `Q` in the terminal to quit: once the prompt has been submitted, the script first clicks **Stop** on the Copilot response and only then closes the browser and exits (exit code 130) without changing the repository. The Stop button selector can be overridden with `COPILOT_STOP` (see [UI selector overrides](#ui-selector-overrides)).

The tool requires a clean working tree by default. `--allow-dirty` bypasses this check, but the uploaded snapshot is still made from `HEAD`, **not** from uncommitted or untracked changes.

## How it works

1. Confirms the target is a Git worktree and checks cleanliness unless `--allow-dirty` is set.
2. Runs `git archive --format=zip HEAD`, saving ZIP bytes under a timestamped `.zip.txt` name (for example `repo-20261024-191212.zip.txt`) for attachment.
3. Opens Copilot Chat, enables **Temporary chat** (unless `--no-temporary-chat`), optionally selects the model, uploads the archive (followed by any `--attach` files), waits for an attachment chip and bounded progress checks (up to 180 seconds), then submits the repair request (retrying for up to 120 seconds).
4. Lets the Copilot UI load for about 2.5 seconds after opening it (the browser is hidden by default), then waits for a downloadable response. The timeout only counts time with no new on-screen output (reasoning or response text) and restarts whenever output appears; if it expires, a screenshot and HTML diagnostics are saved. Reasoning sections (as in the Copilot web UI: header = current step name such as "Reasoning" or "Creating file" while running, "Reasoned for 8m 6s" when done) with their steps (latest 10 lines each), and the intermediate (live) answers written before, between and after them, are streamed live as `ROUNDTRIP_REASONING:` JSON lines on stderr. Code blocks are omitted; "Coding and executing" steps are sent as empty steps (title only, no code).
5. Checks that the download is a ZIP with files, rejects traversal paths, `.git` entries and Unix symlinks, and refuses a byte-identical archive.
6. Backs up all existing worktree files (including untracked files), clears the worktree except `.git`, then copies the complete extracted repository into place. On an apply failure, it restores the backup. It prints Git status, diff statistics, and the latest Copilot assistant chat response (when available) in the JSON output.

**Important:** Files omitted from the returned ZIP are deleted from the worktree; `.git` is preserved. Untracked files are also removed from the worktree (but included in the backup). Source ZIPs, downloads and rollback backups are temporary, kept outside the repository, and deleted when the run finishes (including on errors); the applied changes remain in the working tree. It may add files from the response. It does not independently verify that Copilot changed only what was requested. Review the diff and new files carefully. Files excluded by `git archive HEAD` are not sent to Copilot.

## Review the result

From the target repository:

```powershell
git status
git diff
git diff --check
```

Temporary files are deleted automatically when the run finishes; use `--work-dir` to choose where they are created.

## Command-line options

```text
python vscode-extension/scripts/copilot_repo_roundtrip.py [--profile PATH] [--url URL] login
python vscode-extension/scripts/copilot_repo_roundtrip.py [--profile PATH] [--url URL] fix REQUEST [options]
```

For `fix`, options include:

- `--repo PATH` — target Git repository (default: current directory).
- `--model LABEL` — provider/model pair: `Claude/Opus 5.5` or `GPT/GPT-6` (default: `GPT/GPT-6`).
- `--work-dir PATH` — optional parent folder for temporary archives, downloads and diagnostics (deleted automatically).
- `--backup-dir PATH` — deprecated; rollback copies are temporary and deleted automatically.
- `--timeout SECONDS` — seconds with no new on-screen Copilot output before giving up (default: `900`); new reasoning or response output restarts the timer.
- `--show-browser` — show Chromium during the fix (by default it runs hidden and requires an existing signed-in profile).
- `--headless` — deprecated; the browser is already hidden by default.
- `--allow-dirty` — allow a dirty worktree (use with caution).
- `--temporary-chat` / `--no-temporary-chat` — enable (default) or skip Copilot **Temporary chat**. Temporary chat is enabled before the model is selected, because enabling it resets the model selection.
- `--attach PATH` — additional file or picture to attach to the prompt; repeat for several files.
- `--chat-url URL` — continue the saved Copilot chat at this URL (the `chat_url` of a previous run); implies `--no-temporary-chat`. Runs with Temporary chat off report the chat URL as `ROUNDTRIP_CHAT:` JSON lines on stderr and as `chat_url` in the final JSON.

Use `python vscode-extension/scripts/copilot_repo_roundtrip.py --help` and `python vscode-extension/scripts/copilot_repo_roundtrip.py fix --help` for the CLI help.

## UI selector overrides

If Copilot's interface changes, you can override its Playwright selectors via environment variables:

```powershell
$env:COPILOT_COMPOSER='textarea'
$env:COPILOT_FILE_INPUT='input[type="file"]'
$env:COPILOT_SEND='button[aria-label*="Send" i]'
$env:COPILOT_DOWNLOAD='a[download]'
$env:COPILOT_MODEL_BUTTON='button[aria-label*="model" i]'
$env:COPILOT_TEMPORARY_CHAT='button[aria-label*="temporary" i]'
$env:COPILOT_REASONING='button[aria-expanded][aria-label*="reason" i]'  # plain CSS selector for reasoning headers
$env:COPILOT_STOP='button[aria-label*="Stop" i]'
```

The informational OneDrive upload notice is **not** treated as an upload-progress indicator. Upload readiness is determined from the attachment chip and actual progress elements, with a bounded wait.

## Security notes

- Only use this with repositories whose contents are approved for upload to your organization's Copilot environment.
- Keep the browser profile, downloaded archives and backup directories private.
- Validate the resulting source changes and run your project's tests before committing.
- Browser automation and the download flow are dependent on the current Copilot UI and are not guaranteed to work across updates.
