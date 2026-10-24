# Copilot repo round-trip automation

Experimental browser automation for Microsoft 365 Copilot Chat.

## Install on Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r .\copilot_repo_roundtrip_requirements.txt
python -m playwright install chromium
```

If script activation is blocked:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## Sign in once

```powershell
python .\copilot_repo_roundtrip_fixed.py login
```

Sign in in the Chromium window, reach Copilot Chat, return to PowerShell, and press Enter. The browser profile contains session state and must not be shared or committed.

## Run a repair

Default model selection (`auto` leaves Copilot's current default unchanged):

```powershell
python .\copilot_repo_roundtrip_fixed.py fix `
  "Add a new line to README.md saying hello" `
  --repo "C:\path\to\newjobs"
```

Select a model using its exact visible label:

```powershell
python .\copilot_repo_roundtrip_fixed.py fix `
  "Add a new line to README.md saying hello" `
  --repo "C:\path\to\newjobs" `
  --model "GPT-6"
```

## Fixed attachment ordering

The script now:

1. Selects the requested model before composing the message.
2. Adds the repository archive to the draft.
3. Waits for the attachment chip to appear.
4. Waits for the **Uploading from device** indicator to disappear.
5. Waits one additional second for the draft state to settle.
6. Fills and submits the prompt.

This prevents the prompt from being sent before the attachment upload completes.

## Output and safety

The tool creates the source payload with `git archive HEAD`, so `.git` and untracked files are excluded. It validates the returned ZIP, rejects traversal paths and symlinks, backs up existing tracked files, overlays the returned files, and prints `git status` plus `git diff --stat`. It does not commit or push.

Review every result:

```powershell
git status
git diff
git diff --check
```

## UI selector overrides

Copilot UI changes can break browser automation. Override selectors if needed:

```powershell
$env:COPILOT_COMPOSER='textarea'
$env:COPILOT_FILE_INPUT='input[type=file]'
$env:COPILOT_SEND='button[aria-label*="Send" i]'
$env:COPILOT_DOWNLOAD='a[download]'
$env:COPILOT_MODEL_BUTTON='button[aria-label*="model" i]'
```

The model label must exactly match visible UI text. If `GPT-6` is unavailable to the signed-in account, the script exits instead of silently falling back.


## Nested model menus

The current Copilot UI places **GPT-6** in a secondary OpenAI submenu. The updated selector first opens the main model menu, expands **GPT-5.6 Sol Think deeper** (or another visible OpenAI family row), and then clicks **GPT-6**.

```powershell
python .\copilot_repo_roundtrip_modelmenu_fixed.py fix `
  "Add a new line to readme saying hello" `
  --repo "C:\Users\n38fa\repos\newjobs" `
  --model "GPT-6"
```

## Persistent OneDrive notice fix

The notice **Uploading from device will send a copy to OneDrive (work/school)** is not progress and stays visible after the attachment chip is ready. The script no longer waits for that notice to disappear. It waits for the chip, checks actual progress indicators for no more than 12 seconds, then fills and sends the prompt.
