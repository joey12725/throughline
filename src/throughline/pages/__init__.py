"""Pages: a graph of maintained, cited pages written by the Librarian.

Pages are downstream of lenses. A lens with ``output = "pages"`` (worker
``librarian``) has the same query, prompt, grain, cursor and schedule as any
lens; for each work item the Librarian looks at the evidence and the related
pages and answers with page actions (write, edit, link, unlink, archive).
Pages live in one namespace of slugs; kinds come from a registry (people,
events, places, ... ; a new kind is a candidate). A page is compiled truth over
an append-only timeline (GBrain's page anatomy, adapted from github.com/garrytan/gbrain,
MIT). Each revision is a page_revision record on
channel 'pages' (versioned, cited through provenance, searchable); links tie
pages to each other, to people and to their evidence. /v1/find adds a page
map ranked over that graph. docs/v11/pages.md.
"""

from .graph import related_pages
from .health import channel_health, flagged_items, graph_health, lens_health, page_flags, report
from .librarian import apply_actions, override_model, run_item
from .sinks import export
from .store import CHANNEL, PageError, archive, chronicle, get, kinds, link, merge, neighbors, search, unlink, write

__all__ = [
    "CHANNEL",
    "PageError",
    "apply_actions",
    "archive",
    "channel_health",
    "chronicle",
    "flagged_items",
    "kinds",
    "merge",
    "page_flags",
    "export",
    "get",
    "graph_health",
    "lens_health",
    "link",
    "neighbors",
    "override_model",
    "related_pages",
    "report",
    "run_item",
    "search",
    "unlink",
    "write",
]
