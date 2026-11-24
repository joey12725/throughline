"""yt-dlp runner and error classification.

The runner never uses a shell or the user's yt-dlp config. Its stderr is
classified into ``MediaUnavailable`` (permanent, with a status) or
``MediaTransient`` (retry on a later run) and is never logged or stored.
"""

import shutil
import subprocess

from .errors import MediaTransient, MediaUnavailable


def classify(stderr):
    """Generic classification (Instagram and other sites)."""
    text = stderr.lower()
    if "login required" in text or "rate-limit" in text or "rate limit" in text:
        raise MediaTransient("login required or rate limited")
    if "http error 429" in text or "too many requests" in text:
        raise MediaTransient("rate limited")
    if "private" in text:
        raise MediaUnavailable("private", "private post or account")
    if any(
        marker in text
        for marker in (
            "http error 404",
            "not found",
            "does not exist",
            "has been removed",
            "no longer available",
            "unsupported url",
            "unable to extract",
        )
    ):
        raise MediaUnavailable("dead", "removed or not found")
    raise MediaTransient("yt-dlp failed")


def classify_youtube(stderr):
    """YouTube's messages: members-only and age gates come before generic ones."""
    text = stderr.lower()
    if any(
        marker in text
        for marker in (
            "members-only",
            "members only",
            "join this channel",
            "channel's members",
        )
    ):
        raise MediaUnavailable("members_only", "members-only video")
    if any(
        marker in text
        for marker in (
            "confirm your age",
            "age-restricted",
            "age restricted",
            "inappropriate for some users",
        )
    ):
        raise MediaUnavailable("age_restricted", "age-restricted video")
    if any(
        marker in text
        for marker in (
            "not a bot",
            "http error 429",
            "too many requests",
            "rate-limit",
            "rate limit",
            "premieres in",
            "live event will begin",
            "unable to download webpage",
            "timed out",
        )
    ):
        raise MediaTransient("rate limited, bot check or not yet available")
    if "private video" in text or "video is private" in text:
        raise MediaUnavailable("private", "private video")
    if any(
        marker in text
        for marker in (
            "video unavailable",
            "has been removed",
            "no longer available",
            "account associated with this video has been terminated",
            "does not exist",
            "http error 404",
            "not available in your country",
            "unsupported url",
            "incomplete youtube id",
        )
    ):
        raise MediaUnavailable("removed", "removed or unavailable video")
    raise MediaTransient("yt-dlp failed")


class YtDlp:
    """Runs yt-dlp with fixed options; `command` is an argv prefix."""

    def __init__(
        self,
        cookies=None,
        cookies_from_browser=None,
        binary=None,
        command=None,
        classify=classify,
    ):
        self.cookies = cookies
        self.cookies_from_browser = cookies_from_browser
        self.prefix = list(command) if command else None
        self.binary = binary or (None if command else shutil.which("yt-dlp"))
        self.classify = classify

    @property
    def authenticated(self):
        return bool(self.cookies or self.cookies_from_browser)

    def command(self):
        if self.prefix:
            command = list(self.prefix)
        elif self.binary:
            command = [self.binary]
        else:
            raise MediaTransient("yt-dlp not found on PATH")
        command += ["--ignore-config", "--no-warnings", "--no-progress"]
        if self.cookies:
            command += ["--cookies", str(self.cookies)]
        if self.cookies_from_browser:
            command += ["--cookies-from-browser", self.cookies_from_browser]
        return command

    def run(self, args, timeout):
        try:
            result = subprocess.run(
                self.command() + args, capture_output=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            raise MediaTransient("yt-dlp timed out") from None
        except OSError:
            raise MediaTransient("yt-dlp could not start") from None
        if result.returncode != 0:
            self.classify(result.stderr.decode(errors="replace"))
        return result.stdout
