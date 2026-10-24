# Repo Round-Trip VS Code extension

A sidebar prompt-and-response interface for the repository-wide `copilot_repo_roundtrip.py` workflow. This is **not** an inline coding assistant: each prompt sends a committed Git snapshot to Microsoft 365 Copilot Chat and applies the returned full ZIP to the selected workspace repository.

## Install / run

1. Install Python 3.9+ and Git. On first **Sign in** or **Modify repository**, the extension automatically creates a private Python virtual environment in VS Code extension global storage, installs Playwright and its Chromium browser, and reuses that environment for subsequent runs. Internet access is required for the first setup.
2. Open `vscode-extension/` in VS Code, press **F5** to launch an Extension Development Host. Alternatively run `npm run package` (requires npm access to download `@vscode/vsce`) and install the resulting `.vsix` using **Extensions: Install from VSIX...**.
3. In the Extension Development Host, open your target Git repository folder. Select the **Repo Round-Trip** activity-bar icon.
4. Click **Sign in** (browser visible for interactive authentication), complete browser sign-in, then click **Finish sign in** in the sidebar.
5. Enter the exact provider/model pair (for example `Claude/Opus 5.5` or `GPT/GPT-6`) in the **Model** text field. The slash separates provider from the exact UI model name. Select the repository, type a prompt and click **Modify repository** (the run starts immediately, without a confirmation prompt). The response, Git status and diff summary appear in the chat; execution and setup status appears in **Execution activity** (with diagnostic logs also available in **Output → Repo Round-Trip**).

## Settings

`copilotRoundtrip.pythonPath` (default `python`, used to create the managed virtual environment), `model` (`GPT/GPT-6`), `timeout` (900 seconds), `headless`, `allowDirty`, `profilePath`, `workDir`, `backupDir` are exposed under **Settings → Repo Round-Trip**. For Windows, point `pythonPath` to a Python executable if `python` is not on PATH. The profile, work and backup paths should remain outside the target repo.

## Important limitations

- The bundled Python script is the single canonical automation engine. Browser UI changes may break the flow.
- A clean Git worktree with a valid HEAD is required by default. `allowDirty` does **not** upload uncommitted changes; the archive comes from HEAD.
- The script **replaces the entire worktree** (excluding `.git`) with the downloaded ZIP after backing up files. Files missing from the ZIP are removed, including untracked files. Inspect the Git diff and test before committing.
- The browser sign-in profile contains sensitive authentication state; do not commit or share it.
- No API keys, model API, inline completions, or code assistance features are used. This requires a Microsoft 365 Copilot Chat account and its browser UI.
- Cancel stops the spawned Python process but cannot undo a replacement already in progress; use the backup if needed.
- The extension requires a local VS Code extension host, Python, and Chromium. Remote/web-only hosts are not supported.

Round-trip upload names use local timestamps such as `repo-20261024-191212.zip.txt`. Temporary source ZIPs, downloaded files and rollback backups are deleted when the run finishes (including errors). The applied repository changes remain in the working tree.

The **Show browser** checkbox beside **Sign in** controls subsequent modification runs; it is off by default. The upload can take up to 180 seconds to settle; the send action retries for up to 120 seconds.

During `fix`, the script streams `ROUNDTRIP_STATUS:` JSON lines to stderr for each execution phase (including periodic wait updates); stdout remains the final JSON result. The VS Code sidebar displays these updates live in **Execution activity** as a single rotating line (`Running: <status>`) with a spinner while work is in progress.
