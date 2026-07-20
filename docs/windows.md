# Windows setup

1. Put the kit in the synced folder you intend to keep.
2. In File Explorer, right-click the whole kit and select **Always keep on this device**.
3. Open PowerShell in the kit and run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\bin\install-windows.ps1 -AddToUserPath
```

The installer creates directory junctions for generated agents and the Skill. Junctions track future renders without requiring the same OneDrive path on every machine.

```text
%USERPROFILE%\.claude\agents\delekit
%USERPROFILE%\.claude\skills\orchestrate-delegates
%USERPROFILE%\bin\claudex.cmd
%LOCALAPPDATA%\delekit\device.env
```

Populate only the local `device.env`, including `DELEGATE_PARENT_MODEL`. The
launcher keeps the parent model runtime-selected — an explicit `--model` always
wins — but without that pin a `/model` pick inside a gateway session writes a
gateway-only alias into the **global** settings file and breaks every
non-gateway launch. See [known-issues.md](known-issues.md). Apart from that the
launcher applies the same gateway compatibility defaults as macOS.

Add `ccg` from `bin\ccg-snippet.ps1` to your `$PROFILE`, alongside your existing
launcher rather than replacing it, and pin that existing launcher to its own
model for the same reason.

To run the proxy at login, put a one-line hidden launcher in the Startup folder
(`shell:startup`). Build the command with `Chr(34)` for quoting; nested doubled
quotes fail to parse. macOS uses `bin/install-launchd-macos.sh` instead.

Open a new terminal after adding the user PATH, then run:

```powershell
.\bin\doctor-windows.ps1
```

Execution policy, enterprise controls, filesystem type, or OneDrive status may block junctions or scripts. Do not globally weaken policy. Use `-Copy` or obtain approval for the narrow local change. Copied generated files must be reinstalled after future renders.
