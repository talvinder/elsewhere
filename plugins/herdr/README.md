# Elsewhere for Herdr

Run a build, test, or other command from a Herdr workspace with Elsewhere's
local capacity protection and approved remote execution. Review the project,
command, location, and remote limits before starting. Return to saved remote
jobs to inspect results and verify cleanup.

## Requirements

- Herdr 0.9.0 or newer on macOS or Linux.
- Python 3.11 or newer as `python3`.
- An installed `elsewhere` CLI on PATH (`pipx install elsewhere-run`), or an
  absolute `ELSEWHERE_BIN` pointing at your chosen installation.
- For remote work, an Elsewhere provider configuration and a matching trust
  approval on the machine running the Herdr server. Start with
  `elsewhere doctor` and `elsewhere trust-status` in your project.

This plugin delegates placement, source exclusions, export approval, result
verification, and resource cleanup to Elsewhere. It does not create a second
capacity policy or approve cloud access. Installing the plugin runs no builds,
starts no agents, uploads no source, and allocates no compute.

## Install and open

Once this directory is available on the repository's default branch:

```sh
herdr plugin install talvinder/elsewhere/plugins/herdr
herdr plugin action invoke elsewhere.workloads.open
```

For a development checkout:

```sh
herdr plugin link /absolute/path/to/elsewhere/plugins/herdr
herdr plugin pane open --plugin elsewhere.workloads --entrypoint workloads
```

The pane opens over the active workspace. The project comes from Herdr's
focused-pane context, never from the plugin installation folder. The folder is
printed before the menu and again before execution. No terminal text is treated
as a command automatically.

1. Choose **new workload**, then enter a command and its workload type.
2. Use local execution for commands tied to this machine. For portable remote
   work, provide the appropriate container image, resource/runtime limits, an
   estimated cost, and optional result paths. The image must include the tools
   required by Elsewhere's runner (`timeout`, `tar`, `sha256sum`, `curl`) and your
   command. These settings describe a real environment; the plugin does not
   infer one from the command.
3. Review the location and, for remote work, the existing trust decision. Type
   `run` to start. Enter alone leaves the command unstarted.
4. Inspect the returned output or the saved remote job. Refreshing a finished
   remote job lets Elsewhere recover and verify its results. Cleanup is offered
   only after verified recovery, including when the workload failed.

The reviewed location is pinned for execution. If a local reservation is no
longer available, the command is refused without queuing or silently exporting
source. Request a new plan to consider remote execution.

Optional keybinding in your Herdr configuration:

```toml
[[keys.command]]
key = "prefix+e"
type = "plugin_action"
command = "elsewhere.workloads.open"
description = "Run work with Elsewhere"
```

## Automation

Agents can use the same reviewed workflow without typing into another agent's
terminal. A specification is a JSON object:

```json
{
  "command": "printf 'hello from Elsewhere\\n'",
  "workload": "light",
  "execution": "local"
}
```

```sh
python3 elsewhere_herdr.py plan --source /absolute/project --spec /absolute/job.json
python3 elsewhere_herdr.py run --source /absolute/project --spec /absolute/job.json --execute
```

Run these from this plugin directory. Supply `HERDR_PLUGIN_STATE_DIR` when
calling outside Herdr. Remote specs can additionally set `image`, `provider`,
`cpu`, `memory_mb`, `max_runtime_seconds`, `estimated_cost_usd`, and `result_paths`.
The plugin passes the plan's approval receipt to Elsewhere, which validates it
again before uploading or dispatching. A shell command intentionally runs as
shell code only after execution is authorized; never embed credentials in it.

## Persistence and limits

Receipts are private mode-0600 files under `HERDR_PLUGIN_STATE_DIR`, separated by
project. They retain remote job identifiers and verified completion receipts;
raw provider plans and approval receipts are not copied into plugin storage.
Elsewhere's own ledger remains authoritative. The plugin never cleans another
project's job and never offers a result-discard shortcut.

Remote compute keeps running if you close this pane. Reopen it on the same
Herdr server and choose **saved jobs** to continue. Closing the pane is not
cancellation. Local commands run synchronously in this pane; only their exit
status is retained by this plugin, and displayed output is limited by Elsewhere.

The existing local capacity policy does not enforce the remote CPU/runtime
limits on local commands. Remote costs are estimates, not provider billing caps.
Source contents are packaged at dispatch time, not frozen by a dry plan. Pause
concurrent edits if you need an exact reviewed snapshot; the returned source
fingerprint identifies what was actually transported.

This is not migration of an already-running agent conversation. A different
Herdr server does not inherit this server's Elsewhere ledger or credentials.
Cross-device takeover and Windows are not certified by this plugin.

## Remove

```sh
herdr plugin unlink elsewhere.workloads
```

For a GitHub-managed installation use `herdr plugin uninstall elsewhere.workloads`.
Removing the plugin does not cancel cloud jobs. Recover their results and clean
up through Elsewhere first. Herdr retains plugin-owned config/state; keep receipts
until any active jobs are settled.
