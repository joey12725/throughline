"""Default planner: one round of legs chosen from the request (QRY-15, QRY-21).

Planner protocol (stable; retrieval.planner replaces this module by
presence):

  initial(ctx) -> [leg name]
  next_round(ctx, candidates, round_no) -> ([leg name] or None, stop_reason)
  after_recall(ctx) -> None

A planner never reports that the evidence is sufficient: it only reports why
it stopped retrieving.
"""

MESSAGE_LEGS = ("lexical", "vector", "structured")


class DefaultPlanner:
    name = "default"

    def initial(self, ctx):
        request = ctx.request
        query = (request.query or "").strip()
        if request.legs:
            legs = list(dict.fromkeys(request.legs))
        elif request.exhaustive:
            legs = ["lexical"] if query else ["structured"]
        elif request.strategy == "keyword":
            legs = ["lexical"]
        elif request.strategy == "meaning":
            legs = ["vector"]
        elif request.strategy == "fused":
            legs = ["lexical", "vector"]
        else:
            legs = ["lexical", "vector", "entity"] if query else ["structured"]
            if ctx.message_ids:
                legs.append("links")
            legs += [
                name
                for name, leg in sorted(ctx.legs.items())
                if getattr(leg, "auto", False) and name not in legs
            ]
        if request.raw_only:
            legs = [name for name in legs if name in MESSAGE_LEGS]
        return legs

    def next_round(self, ctx, candidates, round_no):
        return None, "single_round"

    def after_recall(self, ctx):
        return None


PLANNER = DefaultPlanner()
