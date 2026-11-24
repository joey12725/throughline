"""Deprecated name of throughline.feeds (feeds were called consumers).

`import throughline.consumers` returns the throughline.feeds module itself, so
every name (public or private) and every monkeypatch is shared.
"""

import sys

from . import feeds

sys.modules[__name__] = feeds
