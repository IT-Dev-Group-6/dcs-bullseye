# Security Policy

## Supported Versions

Only the latest commit on the `main` branch is actively maintained. No backported security fixes are provided for older releases.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report security issues privately using [GitHub's private vulnerability reporting](https://github.com/IT-Dev-Group-6/dcs-bullseye/security/advisories/new).

Please include:
- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- The affected component (`orchestrator/`, `agent/`, `discord-bot/`, or `scripts/`)
- Any suggested fix if you have one

You can expect an acknowledgement within **3 days** and a fix or status update within **14 days** depending on severity.

## Scope

The following are in scope:

- Authentication and authorization bypass in the orchestrator or agent API
- Path traversal or arbitrary file read/write in the agent
- Invite code or registration abuse allowing unauthorized host registration
- HMAC replay or signature bypass on agent requests
- Secrets exposed in logs, API responses, or installer output

The following are **out of scope**:

- Vulnerabilities requiring physical access to the Windows DCS host
- Issues in third-party dependencies (report those upstream)
- The DCS game client or DCS World itself
- Self-inflicted issues from misconfigured deployments (e.g. running with `api_key` left empty)

## Security Model

- The orchestrator and agent communicate over an frp reverse tunnel; the agent is not publicly exposed
- All orchestrator-to-agent requests are signed with HMAC-SHA256 (`X-Signature`) and include a timestamp + nonce to prevent replay attacks
- Failed authentication attempts are rate-limited (5 failures / 5 min per IP)
- The agent validates all file paths using an allowlist regex and a `is_relative_to()` boundary check
- Audit logs record every destructive action with the Discord user ID of the actor
