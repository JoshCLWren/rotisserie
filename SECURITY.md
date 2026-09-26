# Security Policy

## Project status

Rotisserie is pre-release extraction software. No version is currently
supported for production use. Security fixes are applied to the `main` branch
until the project begins publishing supported releases.

## Reporting a vulnerability

Please do not disclose a suspected vulnerability in a public issue, pull
request, discussion, test fixture, or agent transcript.

Use GitHub's private vulnerability reporting flow for this repository:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Include the affected commit, impact, prerequisites, reproduction steps, and
   any suggested mitigation.

If private reporting is unavailable, contact the repository owner through their
GitHub profile and ask for a private reporting channel without including the
vulnerability details in the initial public message.

You can expect an acknowledgement within seven days. Validation, remediation,
and disclosure timing will depend on severity and exploitability. Good-faith
reporters will be credited if they want credit.

## High-priority security boundaries

Reports are especially valuable when they involve:

- repository or organization mutation outside an explicit allowlist;
- credential exposure to prompts, workers, logs, artifacts, or forks;
- pull-request workflows that execute untrusted code with write credentials;
- stale-revision review or merge authorization;
- lease races that enable duplicate or unauthorized mutation;
- prompt injection that crosses the model/authority boundary;
- dry-run paths that still perform remote mutations;
- command, path, branch, issue-body, or workflow-input injection;
- secret persistence in handoff or recovery state.

## Research guidelines

Use only repositories and accounts you own or have permission to test. Do not
access other users' data, degrade shared services, perform denial of service,
or run autonomous mutations against third-party repositories. Stop testing and
report immediately if you encounter secrets or personal data.
