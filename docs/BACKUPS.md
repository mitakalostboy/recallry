# Local automatic backups

The first successful Knowledge change creates a verified snapshot. The next is due on the first successful change at least 24 hours later. Adding a claim, learning, promotion, rejection and deprecation share this path. Reading Knowledge, a no-op transition and an idle day create no snapshot. The two agents can write concurrently. Per-store and destination locks serialize backup decisions and retention.

The default location is `~/.local/state/recallry/backups/`. Each generation contains a manifest, a standalone `recallry.db` and, when present, the public store's `metrics.db`. The manifest identifies the source store. Keep these directories private. Backups on the same disk protect against a mistaken edit but can be lost together with the disk. No network service is involved.

Recallry verifies SQLite integrity, file checksums and the logical contents before publishing a generation. Retention normally keeps 30 days and at least one additional verified fallback when available. Unrecognized, corrupted and unrelated generations are left alone. A backup failure is reported separately from the already-committed Knowledge change; the next write retries. Store-specific backup status is under `~/.local/state/recallry/backup-settings/`. These settings and the store's consent and configuration are outside the snapshots.

## Recover an independent copy

Use the Python environment containing this release. Choose a completed generation and an existing recovery parent outside your current store, Git checkouts and generation directories. Run the following command with your own three **absolute** paths. The last path is the current Recallry store.

```sh
python - /path/to/recallry-backup-GENERATION /path/to/recovery-parent /path/to/current-store <<'PY'
from pathlib import Path
import sys
from recallry.backup import recover_copy
print(recover_copy(*map(Path, sys.argv[1:])))
PY
```

The command verifies the source, creates a fresh directory named `Recallry-Recovered-…`, checks the copied databases and prints its path. A failed recovery discards its incomplete copy. To inspect it, run `recallry --root /path/printed/above list`. A recovered copy begins with default configuration, unattended review off and no transmission consent. To provision the packaged router templates in the new directory, use `recallry --root /path/printed/above init`. Your active store stays in place. Recovery and concurrent writes were tested with synthetic stores on macOS; Windows locking has not been verified on a Windows machine.
