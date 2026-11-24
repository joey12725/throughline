"""Image keyword leg: api.image_search_records over the images' search text
(kind, tags, caption, place, people, OCR, file name); see legs.visual."""

from ..scope_sql import lit
from . import Leg
from .visual import _candidates, _images_ok, _include_nsfw


class ImageLexical(Leg):
    name = "image_lexical"
    exact = True
    cost_class = "free"
    kinds = ("life_event", "reference", "derived")
    honours_as_of = False
    auto = True
    implements = "image_lexical_search"
    implementation = "postgres_fts_assets"

    def available(self, ctx):
        return _images_ok(ctx)

    def run(self, ctx):
        depth = ctx.depth()
        statement = (
            "SELECT * FROM api.image_search_records("
            + ",".join(
                [
                    lit(ctx.request.query),
                    lit(depth),
                    ctx.args(),
                    lit(ctx.message_ids),
                    lit(_include_nsfw(ctx)),
                ]
            )
            + ")"
        )
        result = ctx.query(statement, cap=ctx.cap(depth), leg=self.name)
        return _candidates(result["rows"], self.name, {})


LEG = ImageLexical()
