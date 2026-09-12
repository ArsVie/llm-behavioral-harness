"""Live, read-only observability for harness runs.

Borrows DeepSeek Harness' context meter and trajectory ideas: the ordered
model-visible surface with one token price per node, reconciled against the
provider's own usage report, and one chronological stream of everything the
run did. Everything here reads; nothing writes, migrates or repairs.

    python -m observability --port 8788
"""

from observability.reader import RunRef, find_runs, repo_root
from observability.server import ObservabilityServer, serve

__all__ = ["RunRef", "find_runs", "repo_root", "serve", "ObservabilityServer"]
