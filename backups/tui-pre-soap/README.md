# Pre-SOAP TUI snapshot

This directory is a read-only historical snapshot of the four-tile OmaRAG TUI
from Git commit `8d24b3fa8d6948997e64bcd2075f2a950184069b`.

It is intentionally outside the Cargo workspace and is not compiled or shipped.
The snapshot contains:

- the original `omarag-tui` crate,
- the matching `omarag-app` state model,
- the original reference screenshots.

The active application lives under the repository's normal `rust/` and `docs/`
paths. To inspect an archived file, open it in place. To restore the historical
implementation, copy only the required files into a separate branch and adapt
them to the current API contracts; do not add this directory to the workspace.
