# Herdr plugin acceptance — 8 September 2026

## Checked workflow

The official Herdr 0.9.0 macOS arm64 binary ran in a temporary configuration and
state directory. The user's normal Herdr registry was not changed. The Elsewhere
runtime used for the remote check matches merged-main commit
`d076d4e5f47ab1ecbd8f058cf1f7fc322b5810c8` byte-for-byte.

| Check | Observed result |
| --- | --- |
| Native manifest registration | Herdr accepted the action and overlay pane |
| Open from a project | Displayed the project folder, not the plugin folder |
| Decline execution review | Returned to the menu without running the command |
| Approve local command | Expected stdout, exit code 0 |
| Reopen local pane | Saved receipt retained exit code 0 |
| Remote bridge execution | One-file source exported to Fly, expected stdout and requested file returned |
| Inspect remote job in Herdr | Saved job showed verified results and exact source fingerprint |
| Cleanup through Herdr | Compute, source upload, and result upload verified absent |
| Read after cleanup | Recovered file still matched the input byte-for-byte |

The remote command copied a text fixture to `output/result.txt` and printed a
marker. Its source fingerprint was
`5bdc30796db976f1468d161ff1e6a6090e35882be9ff3c35a6e02319a2798fe8`.
The result exit code was 0. Requested resources were 1 CPU, 512 MB, and a
120-second maximum runtime, with a declared $0.02 estimate. The recorded elapsed
time was 50 seconds; this is the ledger's observation interval, not a benchmark.

The full project suite includes 18 plugin tests covering context selection,
literal argument forwarding, no execution during planning, refusal without
explicit execution or matching remote approval, a local plan staying local,
durable job identity, bounded following, and cleanup protection for failed or
unverified results. CI exercises the suite on macOS and Linux with Python
3.11–3.13, alongside the existing CLI journey, distribution, and security checks.

## Limits of this evidence

The real remote success was submitted through this plugin's CLI bridge, then
recovered and cleaned through its actual Herdr pane. The interactive remote
input form and automatic follow loop have regression coverage; a second live
remote submission through that form was not performed. Failure and denial
coverage uses fixtures and mocks. This does not certify live Azure execution
through the plugin, arbitrary container images, or cross-device takeover.

The plugin was linked from the development branch for this check. Installation
from the repository's default branch must be checked after merge before sharing
the default-branch installation command as ready to use.
