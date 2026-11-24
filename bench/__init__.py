"""Public-twin benchmark harness.

Everything here is synthetic and seeded. The harness never imports the system
under test: it drives it over HTTP only (a FastAPI TestClient in tests, httpx
from the command line), so the same code scores any deployment.

Modules: scenarios (corpus and questions), gold (evidence labels), score (pure
metrics), arms (retrieval configurations), run (matrix and aggregation),
latency (timed sets and regression gate), scoreboard (append-only results).
"""

HARNESS_VERSION = "1.1.0"
