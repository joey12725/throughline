"""Shared media building blocks for Throughline plugins.

- ``transcribe``: the ``Transcriber`` interface, ``OpenAICompatible`` (primary:
  any ``/v1/audio/transcriptions`` endpoint), ``FasterWhisper`` and
  ``NullTranscriber``.
- ``ffmpeg``: frames at a fixed interval, speech-rate audio, duration, silence.
- ``ytdlp``: the yt-dlp runner and its error classification.
- ``youtube``: YouTube links, metadata, captions and the per-video processor.
- ``assets``: the content-addressed asset store and the outcome cache.
"""

from .errors import BudgetExhausted, MediaTransient, MediaUnavailable

__all__ = ["BudgetExhausted", "MediaTransient", "MediaUnavailable"]
