# Instructions for a local setup agent

Hand this block to a trusted local coding agent to install the kit. It is the
whole contract; the agent needs nothing else except the two browser approvals in
step 6.

```text
This folder is the Claude Delegate Kit.
Install it on this device.

READ FIRST
1. README.md, docs/architecture.md, docs/gateway-setup.md,
   docs/known-issues.md, and the OS doc (docs/macos.md or docs/windows.md).
   known-issues.md lists traps that have already bitten; do not rediscover them.
2. Confirm the whole folder is locally available, not an online-only cloud
   placeholder, and that Claude Code is at least version 2.1.211.

VALIDATE BEFORE INSTALLING
3. Run tools/render_config.py, then tools/verify_kit.py, then every test in
   tests/ for this platform. All must pass before you install anything. If a
   test fails, decide whether the test or the code is wrong, fix the real
   defect, and say which you changed.

INSTALL
4. Run bin/install-macos.sh or bin/install-windows.ps1. Install only under the
   kit's own delegate-kit/orchestrate-delegates names; never overwrite unrelated
   Claude Code agents, skills, settings, or commands.
5. Install CLIProxyAPI from its official releases, verified against the release
   checksums, into a LOCAL directory. The binary, its config.yaml, and its auth
   directory must never live inside the repo. Fill <AUTH_DIR> and
   <CLIENT_KEY> in a copy of generated/cliproxy/config.template.yaml.
6. Run both OAuth logins (-codex-login and -claude-login). They block on
   localhost callbacks and need a browser on this machine; surface the URLs if
   you cannot open them. Afterwards restrict the credential files to owner-only.
7. Start the proxy at login: bin/install-launchd-macos.sh on Mac, a hidden
   Startup-folder launcher on Windows. Verify it survives a restart of the
   process, not just a manual start.
8. Fill the local device.env the installer created. Set DELEGATE_PARENT_MODEL to
   a model the gateway serves; without it a /model pick inside a gateway session
   silently breaks every non-gateway launch on this machine.
9. Add the ccg launcher from bin/ccg-snippet.sh or bin/ccg-snippet.ps1 ALONGSIDE
   the user's existing launcher, never replacing it. A gateway session disables the
   claude.ai connectors, so the direct launcher has to stay available. Pin that
   launcher to its own model for the same reason as step 8.

CONSTRAINTS
10. Never put gateway credentials, OAuth tokens, API keys, or auth files in the
    repo. Populate only the local device.env, or state exactly which values need
    the user's input.
11. Do not set CLAUDE_CODE_SUBAGENT_MODEL. The launcher must remove it so
    per-agent and per-invocation model selection can work.
12. Preserve the launcher's gateway defaults unless the local proxy has been
    tested with a different setting: gateway discovery on, effort forwarding on,
    attribution header off, and tool search false.
13. Keep ccg's environment handling intact — a subshell function on POSIX,
    save/restore on PowerShell. A brace-body rewrite leaks the gateway token
    into the terminal.
14. Do not weaken permissions or use bypass modes merely to make a check pass.
15. Do not duplicate provider model IDs outside config/models.env, and do not
    redesign the alias contract.

VERIFY, THEN REPORT HONESTLY
16. Confirm the generated agents are visible under the Claude config directory,
    run the matching doctor script, and work through the checks at the end of
    docs/gateway-setup.md plus the smoke tests in docs/native-agents.md.
17. A delegate's own reply is NOT evidence it ran on the delegate model. Turn on
    the proxy's debug logging and confirm from the request log that parent and
    delegate requests carry different upstream models. Turn it back off.
18. Report what is verified, what is unverified, and what was skipped. Do not
    call the install complete if any step was partial.

```

The setup agent may adapt paths and shell syntax to the platform.
