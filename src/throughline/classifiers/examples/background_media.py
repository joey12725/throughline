"""Is it media? For recordings with more speakers than the owner, decide how
much is media playing nearby (TV, film, music, audiobook, podcast, short-form
video, YouTube) rather than people talking.

  none     a note on the conversation; nothing is written on messages
  full     every turn gets media_noise {is_noise: true}
  partial  each non-owner speaker is judged on their own; a speaker who is
           media audio gets media_noise on their turns

Attribute: media_noise (core; flagged turns leave default recall and come
back with include_ignored).
"""

from throughline_classify import classifier

KINDS = ["tv", "movie", "music", "audiobook", "podcast", "short_form", "youtube", "game", "other"]

SETTING = (
    "This is a transcript from an always-on recorder the owner wears. Speakers were "
    "separated automatically, so a TV show, film, song, audiobook, podcast, short-form "
    "video (TikTok, Instagram Reels, YouTube Shorts) or YouTube video playing nearby "
    "appears as extra speakers. People talking about a show, or repeating a line from "
    "it, are still real conversation."
)


@classifier(grain="thread")
def classify(thread, ctx):
    others = thread.others
    if not others:
        return []  # only the owner talking: nothing to judge

    # Both questions read the same recording: ask them in one call, so the
    # transcript is sent once (the kind is only used when there is media).
    answers = ctx.jev.ask_many(thread.render(), {
        "level": (
            SETTING + " How much of this recording is media audio rather than real people "
            "talking (in person or on a call)? none: all real conversation. partial: real "
            "conversation with some media audio. full: it is all media audio, with at most "
            "brief reactions from people in the room.",
            ["none", "partial", "full"],
        ),
        "kind": (SETTING + " If media is playing in this recording, what kind is it?", KINDS),
    })
    level = answers["level"]
    p_full = level.probabilities["full"]
    p_any = p_full + level.probabilities["partial"]

    if p_any < 0.5:
        return [thread.note(level="none", confidence=round(1 - p_any, 3))]

    kind = answers["kind"].answer

    if p_full >= 0.6:
        return [thread.mark(is_noise=True, kind=kind, confidence=p_full,
                            reason=f"whole recording is {kind} audio",
                            detail={"level": "full", "kind": kind})]

    marks = []
    media_speakers = []
    for person in others:
        answer = ctx.jev.yes_no(
            SETTING + " Is this participant audio from media playing nearby (TV, film, "
            "music, audiobook, podcast, short-form video, YouTube) rather than a real "
            "person in the conversation?",
            person.render(context=thread),
        )
        if answer.yes:
            media_speakers.append(person.label)
            marks.append(person.mark(is_noise=True, kind=kind, confidence=answer.p,
                                     reason=f"{person.label} is {kind} audio"))
        else:
            marks.append(person.note(media=False, confidence=round(1 - answer.p, 3)))

    marks.append(thread.note(level="partial", kind=kind, confidence=round(p_any, 3),
                             media_speakers=media_speakers))
    return marks
