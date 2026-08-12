# Dogfooding Elsewhere

Elsewhere is in public alpha. Dogfooding should answer one question: can you start
real work, understand where it will run, and safely leave the originating machine?

## Before you start

- Use a non-production cloud project or account.
- Set provider spending alerts.
- Do not begin with a repository containing regulated data or customer secrets.
- Review `.elsewhere.json` and run `elsewhere providers`.
- Install the host signal with `elsewhere sampler-install`, then confirm
  `elsewhere status --human` reports real swap and an explainable capacity band.
- Run every workload as a dry plan before adding `--execute`.

## Six useful runs

1. **Local admission:** keep a `service` preview running, then run a light command
   and confirm settled work is not double-counted forever.
2. **Remote build:** deliberately force a build to Fly or Azure.
3. **Uncommitted source:** transport a small working tree containing a harmless
   `.env` marker and verify it appears in the skipped manifest.
4. **Capacity fallback:** use a provider with multiple configured regions and record
   whether retry works when the preferred region is unavailable.
5. **Results and cleanup:** complete a remote job, recover exact stdout, exit code,
   and one requested file, then verify compute plus source/result artifacts are gone.
6. **Burst protection:** start a memory-heavy local process and confirm bursty work
   waits while swap activity rises, then starts after activity and headroom recover.

## What to record

Use the evidence exporters after a remote job reaches `cleaned`:

```sh
python3 scripts/export_public_run_evidence.py JOB_ID \
  --participant-id participant-1 --scenario success \
  --output /tmp/participant-1-run.json

python3 scripts/capture_public_journey.py \
  --participant-id participant-1 \
  --run-evidence /tmp/participant-1-run.json \
  --source-path . \
  --output /tmp/participant-1-journey.json
```

The second command rechecks the installed version, `doctor`, and a non-billable dry
plan, then links those checks to the verified run, result, and cleanup evidence.

Open a GitHub Discussion or sanitized issue with:

- workload type and approximate size
- local or remote decision and whether it felt correct
- chosen provider and region
- time to start and time to finish
- estimated provider cost
- whether the exact result and requested files returned
- whether cleanup completed
- whether you would have felt safe closing the lid

Never paste signed URLs, credentials, environment values, or private source.

## Current acceptance bar

A run counts as successful when placement is explainable, the workload starts, its
state is observable, secrets are excluded, requested results return with verified
checksums, and cleanup leaves no billable compute or readable source/result artifact.
