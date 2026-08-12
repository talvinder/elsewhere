# Security

Cloud dispatch moves source code outside the laptop. Treat every dispatch as a data
boundary, not merely a faster shell command.

## Defaults

- Planning is non-billable and does not upload source.
- `--execute` is required for cloud mutations.
- Remote execution requires an active trust contract. The contract snapshots the
  provider account, regions, artifact-store account, allowed source roots,
  private/uncommitted-source permission, and per-job limits.
- A receipt fingerprint changes whenever the approved policy changes. Typed Codex
  dispatch requires the current receipt and rejects stale receipts.
- Runtime is enforced around source retrieval, workload execution, result packaging,
  and result upload inside the remote container; images without a `timeout`
  implementation fail closed.
- `.env*`, private-key formats, `.git`, dependencies, and common build output are excluded.
- Symlinks and files larger than 100 MB are excluded.
- Every source bundle contains a manifest with file hashes and skipped-file reasons.
- Tigris and Azure Blob source URLs are read-only and short-lived.
- Result-upload URLs are short-lived and write-only. Download uses the caller's
  authenticated artifact-store identity.
- Result archives reject traversal, links/devices, excessive member counts, expanded
  archives over 250 MB, oversized metadata, and checksum paths outside the cache.
- Cleanup protects an unavailable result by default; discarding it requires the
  explicit `--discard-results` choice.
- Presigned and SAS URLs are redacted before job state is persisted.
- Tokens must never be placed in commands, Git URLs, configuration committed to Git, or logs.
- The optional macOS sampler runs locally every 10 seconds, writes a mode-`0600`
  snapshot under the user's private runtime directory, and performs no network I/O.
- Swap is a brake signal only. Elsewhere never presents disk-backed swap as extra RAM.

## Known limitations

- Pattern-based secret exclusion cannot identify every credential embedded in an
  otherwise ordinary source file. Review the bundle manifest for sensitive projects.
- Cloud images, provider accounts, and networks remain the user's responsibility.
- Private Git repositories require provider-native identity or prebuilt images.
- Object deletion is explicit. Expired presigned URLs stop access but do not delete
  the underlying object.
- The cost ceiling is an estimate gate, not a provider billing cap. CPU, memory,
  runtime, and provider-native budgets remain the hard cost controls.
- Generic shell execution is still subject to the host sandbox's own export policy.
  Install and use the typed Codex integration when a durable, scoped tool boundary
  is required.

## Reporting

Report vulnerabilities through GitHub's private vulnerability reporting for the
repository. Do not open a public issue containing credentials, source URLs, private
source code, or exploit details.
