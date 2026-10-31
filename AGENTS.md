# Repository instructions

Apply these instructions whenever modifying this repository.

- Make only the requested changes. Preserve unrelated files and behavior.
- Always review and clean README files and comments in all repository files after making changes. Keep README content logically correct and aligned with the current repository state. Keep comments concise and free of historical references.
- Explain current intent, constraints, and non-obvious behavior in comments. Remove redundant narration, change logs, and descriptions of previous implementations. Preserve required license notices and tool directives.
- Keep the Python automation in `vscode-extension/scripts/copilot_repo_roundtrip.py` as the single canonical engine used by the CLI and extension.
- Preserve archive validation, Git metadata protection, rollback, cancellation, and authentication-state safeguards.
- Run `npm test` and `python -m unittest discover -s test -p "test_*.py"` from `vscode-extension/`. Report skipped tests or unavailable dependencies; do not claim a live Copilot round-trip was tested without running one.
- Review the final diff for unrelated changes. Never include a `.git` directory in a returned repository archive.
