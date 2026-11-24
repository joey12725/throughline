"""Which email threads are automated? A thread where every sender is a
system is marked as a whole; in a mixed thread only the automated senders'
messages are marked.

Attribute: automated (registered: {is_automated: boolean, score: number}).
"""

from throughline_classify import classifier


@classifier(grain="thread")
def classify(thread, ctx):
    senders = thread.others
    if not senders:
        return []
    marks = []
    automated = []
    for sender in senders:
        answer = ctx.jev.yes_no(
            "Is this sender an automated system (notifications, receipts, newsletters, "
            "alerts, no-reply addresses) rather than a person writing by hand?",
            sender.render(max_chars=6000),
        )
        if answer.yes:
            automated.append(sender)
            marks.append(sender.mark(is_automated=True, confidence=answer.p))
    if automated and len(automated) == len(senders) and not thread.owner:
        return [thread.mark(is_automated=True, confidence=min(m["confidence"] for m in marks),
                            detail={"level": "full"})]
    level = "partial" if automated else "none"
    return marks + [thread.note(level=level)]
