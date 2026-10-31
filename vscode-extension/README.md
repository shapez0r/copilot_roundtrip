# Repo Round-Trip VS Code extension

A sidebar prompt-and-response interface for the repository-wide `copilot_repo_roundtrip.py` workflow. This is **not** an inline coding assistant: each prompt sends a committed Git snapshot to Microsoft 365 Copilot Chat and applies the returned full ZIP to the current workspace repository.

See the repository README (`../README.md` in the source checkout) for command-line usage and archive replacement details.

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
