# szaL16: Stream Zello API with L16 audio

## Motivation

szaL16 is a low-overhead, low-CPU audio streaming protocol using uncompressed audio.
It borrows the Zello Channel API's compact WebSocket stream control and binary framing,
replacing its Opus codec with linear 16-bit PCM. Relays can forward audio payloads without
codec processing; the trade-off is higher bandwidth than compressed audio.

## Protocol difference

Within the streaming portion of the Zello Channel API, szaL16 changes the following:

1. The `codec` value in `start_stream` and `on_stream_start` MUST be `"audio/L16"`
   instead of `"opus"`.
2. Binary audio payloads MUST contain uncompressed, signed 16-bit two's-complement PCM
   samples in network byte order (big-endian), as defined by
   [RFC 2586, audio/L16](https://www.rfc-editor.org/rfc/rfc2586.html).
   Audio is mono; there is no additional channel-count field.

The stream lifecycle, JSON control messages, binary packet header, stream identifiers
and packet identifiers remain unchanged. The `codec_header` remains base64-encoded
with the same format as the original specification.

Audio payloads consist only of PCM samples, mirroring Zello's use of raw Opus frames
without a container.

For live audio, **16 ms packets with one frame per packet are recommended** and used
in the examples below. This aligns with 128-sample processing blocks at 16, 24 and 48 kHz.
Other durations remain supported through the existing duration fields.

This document specifies the stream portion only. Connection establishment, authentication,
channel membership, dispatch and other channel features remain outside its scope.
Inherited channel/routing fields are retained in the adapted reference below; their presence
is not a definition of those wider workflows. Existing Zello services are not presumed to
accept this replacement codec.

## Source and license

The reference sections below are adapted from the Zello Channel API specification,
Copyright (c) 2018 Zello, under the MIT License:

- [Original API documentation](https://github.com/zelloptt/zello-channel-api/blob/main/API.md#streaming-voice-messages).
- [Source revision used for this adaptation](https://github.com/zelloptt/zello-channel-api/blob/4658469e35701994b89e14ef5ab9d69c9c2ee712/API.md).
- [Upstream MIT license](https://github.com/zelloptt/zello-channel-api/blob/4658469e35701994b89e14ef5ab9d69c9c2ee712/LICENSE).
- [Included copy of the upstream license](zello_LICENSE).

Changes comprise the L16 codec/payload definition above and internally consistent
16 ms usage examples. The upstream examples used a 60 ms codec header alongside `packet_duration: 20`; the examples here
use `gD4BEA==` (16 kHz, one 16 ms frame). The four-byte header format itself is unchanged.

## Reference implementations

See the [reference usage notes](reference/README.md) for standalone
[JavaScript Web Audio adapters](reference/szaL16.mjs) and
[Python relay/WAV helpers](reference/szaL16.py). They default to 16 ms and accept other
durations through the existing fields.

## WebSocket control messages

Control messages are JSON-formatted WebSocket text messages; stream data is sent as
WebSocket binary messages. A control request includes `command` and optionally `seq`.
A sequence number is required when a response is expected. Client and server maintain
their own sequence counters for correlating commands with responses.

## Streaming voice messages

After successfully connecting to the channel and receiving [channel status](https://github.com/zelloptt/zello-channel-api/blob/4658469e35701994b89e14ef5ab9d69c9c2ee712/API.md#on_channel_status) you can start sending voice messages. Each message is sent as stream, which begins with `start_stream` command followed by the sequence of binary packets with audio data and ends with `stop_stream` command. szaL16 carries mono [L16](https://www.rfc-editor.org/rfc/rfc2586.html) audio without compression.

### `start_stream`

Starts a new stream to the channel. The successful response includes `stream_id` which must be used in all data packets for this message as well as in `stop_stream` command.

#### Attributes

| Name | Type | Value / Description
|---|---|---
| `command` | string | `start_stream `
| `seq` | integer | Command sequence number
| `channel` | string | The channel to send the message to
| `type` | string | Stream type. Only `audio` is currently supported
| `codec` | string | The name of audio codec used. Required for `audio` streams. Must be `audio/L16`.
| `codec_header` | string | base64-encoded string, representing audio encoding parameters. Required for `audio` streams. See [below](#codec_header-attribute)
| `packet_duration` | integer | Audio packet duration in milliseconds. Values between 2.5 ms and 60 ms are supported
| `call_id` | integer | Optional, dispatchers only: addresses the stream to a [dispatch call](https://github.com/zelloptt/zello-channel-api/blob/4658469e35701994b89e14ef5ab9d69c9c2ee712/API.md#dispatch-calls) the sender holds; the audio goes to that call's user alone
| `broadcast` | boolean | Optional, dispatchers only: sends to every channel user despite active calls. Cannot be combined with `call_id`.
| `for` | string | Optional username to send message to. Other users in the channel won't be receiving this message

##### `codec_header` attribute

`codec_header` is base64-encoded 4 byte array, which represents audio encoding attributes used for the message being sent:

`{sample_rate_hz(16LE), frames_per_packet(8), frame_size_ms(8)}`

| Byte | Value| Description
|---|---|---
|0 & 1 | `sample_rate_hz` | 16 bit little-endian value of audio sample rate in Hz
|2 | `frames_per_packet` | Number of frames per packet (1 or 2)
|3 | `frame_size_ms` | Audio frame size in milliseconds

Example: value of `gD4BEA==` in base64 decodes to `{0x80, 0x3e, 0x01, 0x10}` which represents 16000 Hz sample rate, 1 frame per packet, 16 ms frame size. The decoded header is unchanged in structure from Zello.

#### Request:

```json
{
  "command": "start_stream",
  "seq": 2,
  "channel": "Baker Street 221B",
  "type": "audio",
  "codec": "audio/L16",
  "codec_header": "gD4BEA==",
  "packet_duration": 16
}
```

or

```json
{
  "command": "start_stream",
  "seq": 2,
  "channel": "Baker Street 221B",
  "type": "audio",
  "codec": "audio/L16",
  "codec_header": "gD4BEA==",
  "packet_duration": 16,
  "for": "mrs.hudson"
}
```

#### Response:

```json
{
  "seq": 2,
  "success": true,
  "stream_id": 22695
}
```

### `stop_stream`

Stops outgoing stream. Send this command after you sent the last data packet.

#### Attributes

| Name | Type | Value / Description
|---|---|---
| `command` | string | `stop_stream `
| `seq` | integer | Command sequence number
| `stream_id` | integer | Stream ID as returned in response to `start_stream` command
| `channel` | string | The channel to send the message to


#### Request:

```json
{
  "command": "stop_stream",
  "seq": 3,
  "stream_id": 22695,
  "channel": "Baker Street 221B"
}
```

### Stream data
The same binary packet structure is used for any streamed data (e.g. audio) traveling both ways. The `packet_id` field is populated with the packet number for the audio packets sent from the server to a client. When streaming data to the server the `packet_id` value is ignored and should be filled with zeroes. Fields are stored in network byte order.

`{type(8) = 0x01, stream_id(32), packet_id(32), data[]}`

## Incoming stream events

### `on_stream_start`

Indicates the start of the new incoming stream. This event corresponds to `start_stream` command sent by another channel user.

#### Attributes

| Name                    | Type    | Value / Description
|-------------------------|---------|---
| `command`               | string  | `on_stream_start`
| `type`                  | string  | Stream type. Only `audio` is currently supported
| `codec`                 | string  | The name of audio codec used. Required for `audio` streams. Must be `audio/L16`
| `codec_header`          | string  | base64-encoded codec header buffer. Required for `audio/L16` streams
| `packet_duration`       | integer | Audio packet duration in milliseconds. Values between 2.5 ms and 60 ms are supported
| `stream_id `            | integer | The id of the stream that started
| `channel `              | string  | The name of the channel
| `from `                 | string  | The username of the sender of the message
| `for `                  | string  | The username of the recipient of the message if it was sent with `for` parameter
| `translations_enabled ` | boolean | (optional) Whether translations are enabled for this channel
| `language `             | string  | (optional) The ISO 639-1 language code of the sender
| `call_id `              | integer | (optional) Present when the stream is a `play_dispatch_message` playback: the dispatch call the message belongs to
| `message_id `           | integer | (optional) Present when the stream is a `play_dispatch_message` playback: the played message's id

#### Example:

```json
{
  "command": "on_stream_start",
  "type": "audio",
  "codec": "audio/L16",
  "codec_header": "gD4BEA==",
  "packet_duration": 16,
  "stream_id": 22695,
  "channel": "test",
  "from": "alex",
  "for": "jim"
}
```

### `on_stream_stop`
Indicates the stop of the incoming stream. This event corresponds to `stop_stream` command sent by another channel user.

#### Attributes

| Name | Type | Value / Description
|---|---|---
| `command` | string | `on_stream_stop `
| `stream_id ` | integer | The id of the stream that stopped

#### Example:

```json
{
  "command": "on_stream_stop",
  "stream_id": 22695
}
```
