"""Shared posts: a chat message that shares one post or reel (its text is the
link, or the post's caption and then the link on its own line).

The image attached to such a message is the post's cover (Instagram sends
the reel thumbnail), not a photo anyone in the conversation took. Such links
are stored with role 'preview', tag 'shared-post' and source_ref.shared_url;
their faces never feed the people graph (docs/images.md).

    shared_post_url("https://www.instagram.com/reel/DgA_K5oubuS/\\n\\nreactions: ...")
    -> "https://www.instagram.com/reel/DgA_K5oubuS/"

Lines added by the Beeper import ("reactions: ...") are not message text.
"""

import re

# network -> pattern of a link to one post, reel or video.
PATTERNS = {
    "instagram": r"https?://(?:www\.|m\.)?instagram\.com/(?:[\w.]+/)?(?:p|reels?|tv)/[\w-]+/?(?:\?\S*)?",
    "tiktok": r"https?://(?:(?:www\.|m\.)?tiktok\.com/@[\w.-]+/video/\d+|(?:vm|vt)\.tiktok\.com/\w+|(?:www\.)?tiktok\.com/t/\w+)/?(?:\?\S*)?",
    "youtube": r"https?://(?:www\.|m\.)?youtube\.com/shorts/[\w-]+/?(?:\?\S*)?",
    "x": r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/\w+/status/\d+/?(?:\?\S*)?",
}
_SHARE = re.compile("|".join(f"(?P<{name}>{rx})" for name, rx in PATTERNS.items()), re.IGNORECASE)
# Other link shapes that look like shares, counted but not marked (reports).
OTHER = {
    "instagram_story": r"instagram\.com/stories/",
    "threads": r"threads\.(?:net|com)/@[\w.]+/post/",
    "facebook_share": r"facebook\.com/(?:share|reel|watch)",
}
_URL = re.compile(r"https?://", re.IGNORECASE)
_NOISE = re.compile(r"^\s*reactions:.*$", re.IGNORECASE | re.MULTILINE)


def message_text(content):
    """The message's own text: content without the import's reaction lines."""
    return _NOISE.sub("", content or "").strip()


def shared_post(content):
    """(network, url) when the message is a share of one post or reel: its
    text is one link to it, or ends with that link on a line of its own
    after the post's caption (Beeper's Instagram bridge sends a shared post
    as "<author> <caption>…" and the link). Text with any other link, or with
    the link inside a line of words, is the sender's own message: None."""
    text = message_text(content)
    if not text or len(text) > 3000:
        return None
    lines = text.splitlines()
    match = _SHARE.fullmatch(lines[-1].strip())
    if not match:
        return None
    if len(_URL.findall(text)) != 1:
        return None
    return match.lastgroup, match.group(0)


def shared_post_url(content):
    found = shared_post(content)
    return found[1] if found else None


def other_share(content):
    """The OTHER kind a message's text links to (for reports), or None."""
    text = message_text(content)
    for name, rx in OTHER.items():
        if re.search(rx, text, re.IGNORECASE):
            return name
    return None
