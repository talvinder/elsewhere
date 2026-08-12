# Public readiness evidence

Elsewhere does not claim a remote run is proven because somebody checked a box.
Each qualifying run is exported from the local Elsewhere job ledger only after:

- the remote result bundle was downloaded and its checksums verified;
- the compute resource was verified absent;
- the source artifact was verified absent, when one was uploaded; and
- the result artifact was verified absent.

Export one privacy-safe record after a run reaches `cleaned`:

```bash
python3 scripts/export_public_run_evidence.py JOB_ID \
  --participant-id participant-1 \
  --scenario success \
  --output /tmp/participant-1-run.json
```

The exporter omits the raw job ID, commands, output, account and resource names,
artifact locations, signed URLs, and local paths. It includes hashes for the result
bundle, sanitized job evidence, exporter revision, and an immutable fingerprint of
the acceptance/export code used to create the proof, plus an immutable fingerprint
of the installed Elsewhere runtime that created the job. Source checkouts also record
their clean Git revision. Legacy jobs without that captured provenance cannot certify
a current release. It also distinguishes a source
artifact that was transported and cleaned from a run that did not use source
transport; only the former satisfies the complete Fly/Tigris lifecycle gate. These
records can then be
assembled into `evidence/public-readiness.json` with anonymous participant metadata
and journey completion evidence.

An external participant captures the six-step journey from the same machine after
exporting one of their verified run records:

```bash
python3 scripts/capture_public_journey.py \
  --participant-id participant-2 \
  --run-evidence /tmp/participant-2-run.json \
  --source-path . \
  --output /tmp/participant-2-journey.json
```

This verifies the installed command and version, runs `doctor`, and runs a remote dry
plan without `--execute`. The remaining three steps are linked to the ledger-derived
live-run record, so manually entered journey checkboxes do not satisfy the gate.

Run the complete release gate with:

```bash
python3 scripts/public_readiness.py --release
```

The public-alpha gate requires a complete exact-current Fly compute plus Tigris
artifact lifecycle and one external install-to-cleanup journey. It also verifies the
public history and every logged-out surface advertised to a new user.

Run the broader operational maturity gate with:

```bash
python3 scripts/public_readiness.py --maturity
```

That gate retains the longer-term target: at least three people across macOS and
Linux, at least 25 runs across two compute providers, and three intentional failures.
Do not add provider IDs, personal data, credentials, or raw job ledgers to this
directory.
