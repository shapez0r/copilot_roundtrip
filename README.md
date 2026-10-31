# Copilot Repository Round-Trip

Experimental Python/Playwright automation for submitting a local Git repository to Microsoft 365 Copilot Chat for a requested change, downloading the resulting archive, and applying it to the working tree.

> **Experimental:** This relies on browser UI selectors that can change. Inspect all generated changes before committing. The script does not commit or push.

## Contents

- `vscode-extension/scripts/copilot_repo_roundtrip.py` — command-line tool (`login` and `fix`). This is the single canonical copy of the automation; the VS Code extension in `vscode-extension/` uses the same file (see [VS Code extension](#vs-code-extension)).

## Requirements

- Python 3.9+ (the script uses modern type annotations).
- Git installed and available on `PATH`.
- A Microsoft 365 account with access to Copilot Chat.
- Playwright for Python and its Chromium browser.
- A local Git repository with a valid `HEAD` commit.

## Install (Windows PowerShell)

Run these commands from the repository root. Keep the virtual environment outside the repository so it does not make the working tree dirty or get removed during replacement:

```powershell
py -m venv "$env:USERPROFILE\.copilot-roundtrip-venv"
& "$env:USERPROFILE\.copilot-roundtrip-venv\Scripts\Activate.ps1"
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

The follow-up is sent in that same Copilot chat. The current committed repository state is archived and attached again for every prompt, so follow-ups do not depend on files remaining in Copilot's sandbox. The follow-up prompt tells Copilot that this archive supersedes earlier ones. Commit each accepted round-trip result before sending another prompt, otherwise the next archive still contains the previous `HEAD`. Reasoning, answers and download links of earlier turns are ignored, so only the new response's ZIP is applied. If the saved chat cannot be reopened, the run fails instead of starting a different chat.

While a fix runs, press `Q` in the terminal to quit: once the prompt has been submitted, the script first clicks **Stop** on the Copilot response and only then closes the browser and exits (exit code 130) without changing the repository. The Stop button selector can be overridden with `COPILOT_STOP` (see [UI selector overrides](#ui-selector-overrides)).

The tool requires a clean working tree by default. `--allow-dirty` bypasses this check, but the uploaded snapshot is still made from `HEAD`, **not** from uncommitted or untracked changes.

## How it works

1. Confirms the target is a Git worktree and checks cleanliness unless `--allow-dirty` is set.
2. Runs `git archive --format=zip HEAD`, saving ZIP bytes under a timestamped `.zip.txt` name (for example `repo-20261024-191212.zip.txt`) for attachment.
3. Opens Copilot Chat, enables **Temporary chat** (unless `--no-temporary-chat`), selects the requested model, uploads the archive (followed by any `--attach` files), waits for an attachment chip and bounded progress checks (up to 180 seconds), then submits the repair request (retrying for up to 120 seconds).
4. Lets the Copilot UI load for about 2.5 seconds after opening it (the browser is hidden by default), then waits for a downloadable response. The timeout only counts time with no new on-screen output (reasoning or response text) and restarts whenever output appears; if it expires, a screenshot and HTML diagnostics are written to the temporary run directory and removed when the run exits. They are not retained for later inspection, even with `--work-dir`. Reasoning sections (as in the Copilot web UI: header = current step name such as "Reasoning" or "Creating file" while running, "Reasoned for 8m 6s" when done) with their steps (latest 10 lines each), and the intermediate (live) answers written before, between and after them, are streamed live as `ROUNDTRIP_REASONING:` JSON lines on stderr. Code blocks are omitted; "Coding and executing" steps are sent as empty steps (title only, no code).
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

Temporary files are deleted automatically when the run finishes; use `--work-dir` to choose their parent directory outside the target repository. This does not retain archives, diagnostics or backups after the run.

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

## Tests

Run from `vscode-extension/`:

```powershell
npm test
python -m unittest discover -s test -p "test_*.py"
```

The Node.js tests require Node.js and npm. Python browser tests require Playwright and Chromium; they are skipped when those dependencies are unavailable. `ROUNDTRIP_TEST_CHROMIUM` can point to an existing Chromium executable for the browser tests. These regression tests do not verify a live Copilot sign-in or round-trip.

## Security notes

- Only use this with repositories whose contents are approved for upload to your organization's Copilot environment.
- Keep the browser profile, downloaded archives and backup directories private.
- Validate the resulting source changes and run your project's tests before committing.
- Browser automation and the download flow are dependent on the current Copilot UI and are not guaranteed to work across updates.

---

## VS Code extension

A sidebar prompt-and-response interface for the repository-wide `copilot_repo_roundtrip.py` workflow. This is **not** an inline coding assistant: each prompt sends a committed Git snapshot to Microsoft 365 Copilot Chat and applies the returned full ZIP to the current workspace repository.

The command-line workflow and archive replacement details are documented above.

## Install / run

1. Install VS Code 1.85.0 or later, Python 3.9+ and Git. Install Node.js and npm to package the extension or run its Node.js tests. On first **Sign in** or **Modify repository**, the extension automatically creates a private Python virtual environment in VS Code extension global storage, installs Playwright and its Chromium browser, and reuses that environment for subsequent runs. Internet access is required for the first setup.
2. From `vscode-extension/`, run `npm run package` (requires npm access to download `@vscode/vsce`) and install the resulting `.vsix` using **Extensions: Install from VSIX...**. For development, run `code --extensionDevelopmentPath="<absolute path to vscode-extension>" "<absolute path to target repository>"` to open an Extension Development Host. This repository does not include an F5 launch configuration.
3. In VS Code (or the Extension Development Host), open your target Git repository folder (the run always uses the current workspace repository: the folder of the active editor, otherwise the first workspace folder). Select the **Repo Round-Trip** activity-bar icon.
4. Click **Sign in** (browser visible for interactive authentication), complete browser sign-in, then click **Finish sign in** in the sidebar.
5. Enter the exact provider/model pair (for example `Claude/Opus 5.5` or `GPT/GPT-6`) in the **Model** field: its dropdown offers `GPT/GPT-6` and `Claude/Opus 5.5`, and the field stays editable so any other provider/model can be typed. The slash separates provider from the exact UI model name. Type a prompt and click **Modify repository** (the run starts immediately, without a confirmation prompt). The response appears in the chat, followed by separate **Changes** and **Git status** sections with color-coded file paths (green/red/yellow for added/deleted/modified) plus colored status squares and a compact bar of at most five green/red squares per file, regardless of change size; open **Source Control** and select a changed file there to inspect its diff (the sidebar result rows are not clickable); execution and setup status appears in **Execution activity** (with diagnostic logs also available in **Output → Repo Round-Trip**).
6. To attach additional files to the prompt, paste an image (Ctrl+V / Cmd+V) into the prompt box, drag and drop images or files onto the prompt area (VS Code requires holding **Shift** while dropping into a webview; files can be dropped from the operating system or the VS Code Explorer), or click **Attach files...** to pick files. Attachments appear as removable chips (images with a thumbnail) and are uploaded alongside the repository archive as context; they are not added to the repository. Pasted or dropped file contents are written to a temporary folder that is deleted after the run.

## Chat history

The sidebar keeps a chat history like a regular AI coding-assistant extension. **New chat** (also in the view title bar) starts a conversation, and **History** lists the saved chats of the current workspace (newest first, with time, number of prompts and model). Click a chat to reopen it, or **×** to remove it from the history (the Copilot conversation itself is not deleted).

- A chat whose first prompt is run with **Temporary chat** off is saved in the history (per workspace, kept across VS Code restarts). With Temporary chat on, the conversation is not saved and every prompt runs in a new Copilot temporary chat.
- The Temporary chat choice is fixed after a chat's first prompt; use **New chat** to change it.
- Every subsequent prompt of a saved chat is executed in the same Copilot chat in the web UI: the extension records the Copilot chat URL after the first prompt and reopens it for each follow-up.
- Each prompt attaches the current committed repository state again, so follow-ups do not depend on files remaining in Copilot's sandbox. Follow-ups tell Copilot that this archive replaces earlier ones. Commit accepted changes before the next prompt; otherwise a clean-tree check fails, or `allowDirty` sends the old `HEAD` rather than those changes.
- If the Copilot chat link could not be recorded (for example when the first run failed before Copilot opened the conversation), the next prompt starts a new Copilot chat for that history entry.

## Settings

All settings use the `copilotRoundtrip.` prefix and appear under **Settings > Repo Round-Trip**:

| Setting | Default | Purpose |
| --- | --- | --- |
| `pythonPath` | `python` | Python executable used to create the managed virtual environment. |
| `model` | `GPT/GPT-6` | Initial provider/model value in the editable sidebar field. |
| `timeout` | `900` | Inactivity timeout in seconds; new on-screen output restarts it. |
| `showBrowser` | `false` | Initial state of **Show browser** for modification runs; sign-in is always visible. |
| `temporaryChat` | `true` | Initial state of **Temporary chat** for new chats. |
| `allowDirty` | `false` | Bypass the clean-tree check; uploads still use `HEAD` only. |
| `profilePath` | Empty | Browser profile path; empty uses `~/.copilot-repo-browser`. |
| `workDir` | Empty | Parent directory for temporary run files; files are deleted when the run exits. |
| `backupDir` | Empty | Legacy setting passed as deprecated `--backup-dir`; it does not retain backups or change their location. |

On Windows, set `pythonPath` to a Python executable if `python` is not on `PATH`. Keep the profile and temporary work directory outside the target repository. There is no `copilotRoundtrip.headless` setting; use `showBrowser` instead. Sidebar model and checkbox changes apply to runs but do not update the stored VS Code settings.

## Important limitations

- The bundled Python script is the single canonical automation engine. Browser UI changes may break the flow.
- A clean Git worktree with a valid HEAD is required by default. `allowDirty` does **not** upload uncommitted changes; the archive comes from HEAD.
- The script **replaces the entire worktree** (excluding `.git`) with the downloaded ZIP after backing up files. Files missing from the ZIP are removed, including untracked files. Inspect the Git diff and test before committing.
- The browser sign-in profile contains sensitive authentication state; do not commit or share it.
- No API keys, model API, inline completions, or code assistance features are used. This requires a Microsoft 365 Copilot Chat account and its browser UI.
- Cancel first clicks **Stop** on the running Copilot response, then quits the script (same as pressing `Q` in a terminal run); the process is only killed if it has not exited after 30 seconds. Cancel cannot undo a replacement already in progress. Rollback backups are temporary and deleted when the run exits, so keep an independent backup of any work you need to retain.
- Press **Enter** in the prompt box to run it, exactly like clicking **Modify repository**; **Shift+Enter** inserts a new line.
- The extension requires a local VS Code extension host, Python, and Chromium. Remote/web-only hosts are not supported.

Round-trip upload names use local timestamps such as `repo-20261024-191212.zip.txt`. Temporary source ZIPs, downloaded files and rollback backups are deleted when the run finishes (including errors). The applied repository changes remain in the working tree.

The **Show browser** checkbox beside **Sign in** controls subsequent modification runs; it is off by default. The **Temporary chat** checkbox (on by default) enables Copilot's Temporary chat before the model is selected; untick it to use a normal saved chat. The upload can take up to 180 seconds to settle; the send action retries for up to 120 seconds.

During `fix`, the script streams `ROUNDTRIP_STATUS:` JSON lines to stderr for each execution phase (including periodic wait updates); stdout remains the final JSON result. The VS Code sidebar displays these updates live in **Execution activity** as a single rotating status line with a spinner while work is in progress.

After opening Copilot Chat, the script waits about 2.5 seconds (with or without **Show browser**) so the Copilot UI loads properly before it is used. While Copilot works, its reasoning is streamed live (ROUNDTRIP_REASONING: JSON lines on stderr) under **Execution activity** and laid out like the Copilot web UI. Each reasoning section is one collapsible header: while it runs, the header shows the current step name (for example "Reasoning", "Creating file" or "Coding and executing") with an activity icon, and the collapsed header previews that step's latest text below it; when it finishes, the header becomes "Reasoned for 8m 6s". Expanding a section lists its steps (check icon, step name and text, latest 10 lines each), including the last "Reasoning" step that repeats the interim answer. "Coding and executing" steps are shown as empty, collapsible steps without any code: expanding one only shows "AI coded something here, hidden for better viewing purposes. And basically who cares". Code blocks are never streamed. Sections start collapsed, as in the web UI, and your expand/collapse choices are kept while the text updates. The model's intermediate (live) answers - the response text Copilot writes before, between and after those sections - are shown in on-screen order between them (answer, "Reasoned for 38s", answer, "Reasoned for 4s", final answer). As in the web UI, the short interim answers Copilot writes between the "Reasoned for ..." sections (for example "Now the Python scraper changes." / "Reasoned for 5s" / "Now extension.js parsing." / "Reasoned for 3s") are shown in full as plain text lines above their section; they are read from every Copilot message element of the current turn, not only the latest one. Text that the web UI keeps in the page but does not display (collapsed panels, earlier placeholders such as "Taking a look...") is ignored, and once the result ZIP is downloaded the script waits for Copilot to finish its response and replaces the live answers with the final on-screen layout, including the final answer after the last section.

Git commands started by the script run with stdin redirected to the null device (and `GIT_TERMINAL_PROMPT=0`). The script's stdin pipe is reserved for the Cancel/Q channel; on Windows, letting Git inherit that pipe while it is being read caused runs to hang at "Checking repository and Git working tree...".

## Tests

Run from `vscode-extension/`:

```powershell
npm test
python -m unittest discover -s test -p "test_*.py"
```

Python browser tests require Playwright and Chromium and are skipped if either is unavailable. Set `ROUNDTRIP_TEST_CHROMIUM` to use an existing Chromium executable. These tests do not exercise a live Copilot account.
