# Public alpha release gate

- [x] Apache 2.0 license and NOTICE
- [x] Current tree and all locally reachable commit snapshots scanned for key files,
      private keys, signed URLs, and private provider values
- [x] Personal provider configuration remains ignored
- [x] Security boundary and private reporting path documented
- [x] CI covers Python 3.11, 3.12, and 3.13 on macOS and Linux
- [x] Contribution and conduct guidance
- [x] Bug and provider issue templates
- [ ] A stranger completes install, doctor, dry plan, live run, result recovery, and cleanup
- [x] Dogfood protocol documented
- [x] Alpha limitations stated in README
- [x] Enable GitHub private vulnerability reporting after repository becomes public
- [ ] Exact release commit succeeds on the complete CI matrix
- [ ] At least 25 current runs from three people cover macOS and Linux
- [ ] Fresh Fly/Tigris evidence includes source transport, result recovery, cost,
      region, and verified compute/source/result cleanup
- [ ] Public Git history contains no internal strategy or provider identifiers
- [ ] All logged-out landing-page, repository, install, and dogfood links work
- [ ] Create the next public release tag only after every current gate passes

Run `python3 scripts/public_readiness.py --release` for the executable gate. The
release form performs anonymous network requests, without provider or GitHub
credentials, to verify the landing page, repository, Git install endpoint, and
dogfood guide advertised to a stranger. Its
evidence file is `evidence/public-readiness.json`; the capture workflow is documented
in [`evidence/README.md`](../evidence/README.md). Participants must have stable
anonymous IDs, `macos` or `linux`, and a role of `maintainer` or `external`. Every
qualifying run is exported from the Elsewhere job ledger after checksum-verified
result recovery and verified cleanup. Complete Fly proof must use Tigris for both
source and result artifacts, and also record region and a positive cost estimate.
At least one external participant must record all six journey steps: `install`,
`doctor`, `dry_plan`, `live_run`, `result_recovery`, and `cleanup`.

## Audit note

The current tree contains no tracked `.env`, private-key, certificate, or personal
provider configuration. History sanitization remains an explicit unchecked gate
above; current-tree cleanliness must not be presented as proof that every historical
ref or repository-host pull-request object is public-safe.

The complete Fly/Tigris run on 2026-08-12 transported the committed source
through Tigris, executed on Fly in `sin`, recovered the exact result through Tigris,
verified the result checksum, and verified compute/source/result cleanup at a `$0.02`
estimate. Subsequent pre-release hardening changed the runtime lifecycle, trust
preflight, cleanup, and result verification code, so that run remains valid evidence
for its recorded revision but no longer certifies the exact current runtime. The gate
now fails when runtime code has changed since the proven revision. No additional
billable run has been made. The earlier partial run remains excluded from public
evidence. During the same audit, a stopped Fly Machine retained from an August 9
Elsewhere build was found without a matching job in the discovered local ledger.
After its exact identity and stopped state were re-verified, the maintainer explicitly
approved its destruction; the Fly app was then independently verified to contain
zero Machines.
