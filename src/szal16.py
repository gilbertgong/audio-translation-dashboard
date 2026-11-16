"""Construct the szaL16 JSON messages used by the application.

Return plain dictionaries; callers own IDs, codec headers, JSON serialization and
transport. No parsing, validation, stream state or inference dependencies.
Application extensions are identified below rather than implied to be Zello fields.
"""


def success_response(seq, stream_id=None):
    """Acknowledge a command; stream_id is supplied when starting a stream."""
    result = dict(seq=seq, success=True)
    if stream_id is not None:
        result["stream_id"] = stream_id
    return result


def on_stream_start(*, channel, stream_id, codec, codec_header, packet_duration, sender, message_id=None):
    """Announce an audio stream, optionally linking it to a recorded message."""
    result = dict(command="on_stream_start", channel=channel, stream_id=stream_id,
                  type="audio", codec=codec, codec_header=codec_header,
                  packet_duration=packet_duration, **{"from": sender})
    if message_id is not None:
        result["message_id"] = message_id
    return result


def on_stream_stop(stream_id):
    return dict(command="on_stream_stop", stream_id=stream_id)


def translation(language, message):
    """One translations entry; language is a language code."""
    return dict(language=language, message=message)


def on_transcription(*, stream_id, text, language, translations, stt_seconds=None, llm_seconds=None):
    """Transcription and translations associated with a stream.

    stt_seconds and llm_seconds are optional application timing extensions,
    not Zello fields. Preserve caller-provided precision; omit timing extensions
    when unspecified.
    """
    result = dict(command="on_transcription", stream_id=stream_id,
                  text=text, language=language, translations=translations)
    if stt_seconds is not None:
        result["stt_seconds"] = stt_seconds
    if llm_seconds is not None:
        result["llm_seconds"] = llm_seconds
    return result


def on_error(error, *, message_id=None, stream_id=None):
    """Report an error; optional message_id/stream_id are application correlation extensions."""
    result = dict(command="on_error", error=error)
    if message_id is not None:
        result["message_id"] = message_id
    if stream_id is not None:
        result["stream_id"] = stream_id
    return result
