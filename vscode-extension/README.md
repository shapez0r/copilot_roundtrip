# Repo Round-Trip VS Code extension

A sidebar prompt-and-response interface for the repository-wide `copilot_repo_roundtrip.py` workflow. This is **not** an inline coding assistant: each prompt sends a committed Git snapshot to Microsoft 365 Copilot Chat and applies the returned full ZIP to the selected workspace repository.

## Install / run

1. Install Python 3.9+ and Git. On first **Sign in** or **Modify repository**, the extension automatically creates a private Python virtual environment in VS Code extension global storage, installs Playwright and its Chromium browser, and reuses that environment for subsequent runs. Internet access is required for the first setup.
2. Open `vscode-extension/` in VS Code, press **F5** to launch an Extension Development Host. Alternatively run `npm run package` (requires npm access to download `@vscode/vsce`) and install the resulting `.vsix` using **Extensions: Install from VSIX...**.
3. In the Extension Development Host, open your target Git repository folder. Select the **Repo Round-Trip** activity-bar icon.
4. Click **Sign in**, complete browser sign-in, then press Enter in the VS Code terminal.
5. Enter a model label (for example `GPT-6`) in the **Model** field beside **Sign in**, or leave `auto` to keep the current model. Select the repository, type a prompt, click **Modify repository**, and confirm the warning. The response, Git status, diff summary, backup location and result ZIP appear in the chat; detailed logs appear in **Output → Repo Round-Trip**.

## Settings

`copilotRoundtrip.pythonPath` (default `python`, used to create the managed virtual environment), `model` (`auto`), `timeout` (900 seconds), `headless`, `allowDirty`, `profilePath`, `workDir`, `backupDir` are exposed under **Settings → Repo Round-Trip**. For Windows, point `pythonPath` to a Python executable if `python` is not on PATH. The profile, work and backup paths should remain outside the target repo.

## Important limitations

- The bundled Python script is a copy of the repository's existing script, not a rewritten automation engine. Browser UI changes may break the flow.
- A clean Git worktree with a valid HEAD is required by default. `allowDirty` does **not** upload uncommitted changes; the archive comes from HEAD.
- The script **replaces the entire worktree** (excluding `.git`) with the downloaded ZIP after backing up files. Files missing from the ZIP are removed, including untracked files. Inspect the Git diff and test before committing.
- The browser sign-in profile contains sensitive authentication state; do not commit or share it.
- No API keys, model API, inline completions, or code assistance features are used. This requires a Microsoft 365 Copilot Chat account and its browser UI.
- Cancel stops the spawned Python process but cannot undo a replacement already in progress; use the backup if needed.
- The extension requires a local VS Code extension host, Python, and Chromium. Remote/web-only hosts are not supported.
