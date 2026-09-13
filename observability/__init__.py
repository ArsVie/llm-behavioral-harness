"""Live, read-only observability for harness runs.

The priced model-visible surface plus one chronological stream of everything
the run did. Nothing here writes, migrates or repairs.

python -m observability --port 8788
"""

from observability.reader import RunRef, find_runs, repo_root
from observability.server import ObservabilityServer, serve

__all__ = ["RunRef", "find_runs", "repo_root", "serve", "ObservabilityServer"]
