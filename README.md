# Copilot Repository Round-Trip

Experimental Python/Playwright automation that sends a committed Git repository snapshot to Microsoft 365 Copilot Chat, downloads a complete replacement ZIP, and applies it to the local working tree. A VS Code sidebar uses the same Python engine as the command-line tool.

> **Experimental:** The workflow depends on Copilot's browser UI and selectors. Review all changes before committing. It does not commit or push.

## Requirements

- Python 3.9+, Git, and a local Git repository with a valid `HEAD` commit.
- A Microsoft 365 account with access to Copilot Chat.
- Playwright for Python and its Chromium browser.
- VS Code 1.85+ for the extension; Node.js and npm for packaging and JavaScript tests.

## Command-line setup

From the repository root in Windows PowerShell, create a virtual environment **outside** the repository. This keeps the worktree clean and prevents the replacement process from removing the environment.

```powershell
py -m venv "$env:USERPROFILE\.copilot-roundtrip-venv"
& "$env:USERPROFILE\.copilot-roundtrip-venv\Scripts\Activate.ps1"
python -m pip install --upgrade pip playwright
python -m playwright install chromium
```

If activation is blocked, use `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` for that PowerShell session. There is no requirements file in this repository.

### Sign in

```powershell
python .\vscode-extension\scripts\copilot_repo_roundtrip.py login
```

Sign in using the visible Chromium window, navigate to a usable Copilot Chat page, and press Enter in the terminal. The persistent profile defaults to `~/.copilot-repo-browser`; it may contain authentication state and must remain private and outside version control.

### Request changes

```powershell
python .\vscode-extension\scripts\copilot_repo_roundtrip.py fix "Update the README" --repo "C:\path\to\repository"
```

The default model is `GPT/GPT-6`. Specify another available provider/model label with `--model "Claude/Opus 5.5"` (or another exact UI label). Model availability and menu structure can change.

Attach context files using repeatable `--attach PATH` options. These files are uploaded with the repository snapshot but are not added to the repository.

To continue a saved chat, disable Temporary chat on the initial run with `--no-temporary-chat`, then use the returned `chat_url`:

```powershell
python .\vscode-extension\scripts\copilot_repo_roundtrip.py fix "Update the tests" --repo "C:\path\to\repository" --chat-url "https://m365.cloud.microsoft/chat/..."
```

Every request, including a follow-up, uploads a fresh archive of **committed `HEAD`**, not uncommitted changes. Commit an accepted result before the next request. A saved-chat follow-up reuses the same Copilot conversation and ignores earlier response links. If that conversation cannot be reopened, the run fails rather than starting a different one.

Press **Q** to cancel a CLI run. After submission, cancellation attempts to click Copilot's **Stop** control before exiting without applying changes.

### Options

Run `python vscode-extension/scripts/copilot_repo_roundtrip.py --help` or append `fix --help` for full usage.

- `--repo PATH`: target repository (default: current directory).
- `--model LABEL`: exact provider/model label (default: `GPT/GPT-6`).
- `--attach PATH`: additional context file; repeat as needed.
- `--timeout SECONDS`: inactivity timeout (default: 900); visible output or task-card changes reset it.
- `--show-browser`: display Chromium during a fix; sign-in always uses a visible browser.
- `--allow-dirty`: bypass the clean-tree check; the uploaded snapshot still comes from `HEAD`.
- `--temporary-chat` / `--no-temporary-chat`: use a temporary chat (default) or a saved chat.
- `--chat-url URL`: continue a saved chat; implies `--no-temporary-chat`.
- `--work-dir PATH`: parent for temporary files, outside the target repository; run files are deleted on exit.
- `--profile PATH` and `--url URL`: global browser profile and Copilot Chat URL options, placed before `login` or `fix`.
- `--headless` and `--backup-dir`: deprecated compatibility options. The browser is hidden by default, and rollback backups are always temporary.

### Archive and safety behavior

1. Verify the target Git worktree and, unless `--allow-dirty` is set, require it to be clean.
2. Run `git archive --format=zip HEAD` and upload the ZIP bytes with a `.zip.txt` filename, followed by optional context attachments.
3. Open Copilot Chat, configure Temporary chat and model selection, wait for attachment readiness, and submit the request.
4. Wait for the response to finish before discovering a downloadable ZIP. Activity and task-card updates reset the inactivity timer.
5. Validate that the returned archive contains files and reject unsafe paths, `.git` entries, and Unix symlinks. A byte-identical returned archive is rejected.
6. Back up the entire worktree, including untracked files, then replace everything except `.git` with the returned repository. Restore the backup if applying the result fails.

**Important:** A returned ZIP is a **full replacement**, not a patch. Files omitted from it are deleted from the worktree, including untracked files. Files excluded from `git archive HEAD` are not uploaded. Backups, downloads, and timeout diagnostics are temporary and deleted at the end of a run, including when `--work-dir` is used. Keep an independent backup of important work.

The tool reports Git status and diff statistics, but does not verify that Copilot changed only the requested files. From the target repository, review:

```powershell
git status
git diff
git diff --check
```

### UI selector overrides

The following environment variables override Playwright selectors when Copilot's UI changes:

```powershell
$env:COPILOT_COMPOSER='textarea'
$env:COPILOT_FILE_INPUT='input[type="file"]'
$env:COPILOT_SEND='button[aria-label*="Send" i]'
$env:COPILOT_DOWNLOAD='a[download]'
$env:COPILOT_MODEL_BUTTON='button[aria-label*="model" i]'
$env:COPILOT_TEMPORARY_CHAT='button[aria-label*="temporary" i]'
$env:COPILOT_REASONING='button[aria-expanded][aria-label*="reason" i]'
$env:COPILOT_STOP='button[aria-label*="Stop" i]'
```

`COPILOT_STOP` is used for both completion detection and cancellation. Current task cards take priority over the composer Stop control. Upload readiness depends on attachment indicators and bounded progress checks, not the informational OneDrive notice.

## VS Code extension

The extension in `vscode-extension/` provides a sidebar for repository-wide changes, not inline code completions. Its single automation engine is `scripts/copilot_repo_roundtrip.py`.

### Install and use

1. From `vscode-extension/`, run `npm run package` (requires access to `@vscode/vsce`) and install the generated `.vsix` with **Extensions: Install from VSIX...**. For development, launch an Extension Development Host using `code --extensionDevelopmentPath="<absolute path to vscode-extension>" "<absolute path to target repository>"`. No F5 launch configuration is included.
2. Open a local Git repository in VS Code and select **Repo Round-Trip** in the activity bar. The active editor's workspace folder is used, or the first local workspace folder if none is active.
3. Open **Chat options** beside Attach and Model, choose **Sign in**, authenticate in Chromium, then click **Finish sign in**. The extension installs Playwright and Chromium into a private Python environment in VS Code global storage on first use; initial setup requires internet access.
4. Choose or type an available provider/model label (presets include `GPT/GPT-6`, `GPT/GPT-6.1 Sol`, and `Claude/Opus 5.5`). Commit, stash, or discard all staged, unstaged, and untracked changes first. The sidebar disables **Modify repository** and Enter submission until Git status is clean, and checks again immediately before starting. Enter a request and click **Modify repository**, or press Enter. Shift+Enter inserts a newline. The operation begins without a confirmation prompt.
5. Inspect the **Changes** summary and the actual diff in VS Code Source Control. Diagnostic output is available under **Output → Repo Round-Trip**.

Use **Attach files...**, paste images, or drop files into the prompt area (hold Shift when dropping into the VS Code webview). Attachments are temporary context, not repository files. The sidebar streams visible response text, task plans, and activity; code-execution details are omitted. The active task card's Stop action and the composer Cancel control request cancellation before terminating the process if necessary.

### Chat history

- **New chat** starts a separate conversation. **History** lists saved chats for the current workspace and allows reopening or removing a history entry; removing it does not delete the Copilot conversation.
- Temporary chat is on by default and is not saved. Turn it off before the first prompt to save the chat across VS Code restarts. This choice is fixed for that chat.
- Saved follow-ups reopen the recorded Copilot chat URL and upload the current committed snapshot again. If the first run never recorded a URL, the next prompt starts a new Copilot conversation for that history entry.
- Commit accepted changes before follow-ups. The sidebar requires a clean worktree even when the `allowDirty` setting is enabled; the CLI `--allow-dirty` option still archives only `HEAD`.

### Settings

All settings are under **Settings → Repo Round-Trip** with the `copilotRoundtrip.` prefix:

| Setting | Default | Purpose |
| --- | --- | --- |
| `pythonPath` | `python` | Python executable used to create the managed environment. |
| `model` | `GPT/GPT-6` | Initial editable model label. |
| `timeout` | `900` | Seconds without new Copilot output or task-card changes. |
| `showBrowser` | `false` | Initial browser visibility for modification runs. |
| `temporaryChat` | `true` | Initial Temporary chat choice for new chats. |
| `allowDirty` | `false` | CLI compatibility setting; the sidebar always requires a clean worktree. |
| `profilePath` | empty | Browser profile path; defaults to `~/.copilot-repo-browser`. |
| `workDir` | empty | Parent directory for automatically deleted run files. |
| `backupDir` | empty | Deprecated compatibility setting; backups remain temporary. |

Sidebar model and checkbox choices apply to runs without changing stored VS Code settings. The extension requires a local extension host, Python, and Chromium; remote/web-only hosts are unsupported.

## Tests

Run from `vscode-extension/`:

```powershell
npm test
python -m unittest discover -s test -p "test_*.py"
```

Python browser tests require Playwright and Chromium and are skipped if unavailable. Set `ROUNDTRIP_TEST_CHROMIUM` to an existing Chromium executable if needed. These regression tests do **not** exercise a live Copilot sign-in or repository round-trip.

## Security and maintenance

- Upload only repositories approved for your organization's Copilot environment. Keep browser profiles and temporary archives private.
- Review the resulting diff and test the target project before committing. Browser UI changes can interrupt automation.
- Follow `AGENTS.md` and `.github/copilot-instructions.md` when present. Keep README files accurate and comments concise, focused on current behavior, and free of historical narration; preserve license notices and tool directives.
- The Python script remains the sole automation engine for both CLI and extension. Repository instructions included in prompts are guidance, not an enforcement mechanism.
