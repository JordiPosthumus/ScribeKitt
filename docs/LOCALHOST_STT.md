# Localhost speech API

ScribeKitt's Swift UI already owns a long-lived Python ML daemon. The HTTP listener
runs **inside that daemon**, using `MODEL_CACHE[("parakeet",
"mlx-community/parakeet-tdt-0.6b-v2")]`. HTTP code never calls the model loader.
No separate model server, subprocess for inference, weight copy, decoder setting
override, attention change, or GPU idle warm loop is introduced.

## Start and configure

The listener starts with the app once the local runtime has been prepared. It
binds IPv4 **127.0.0.1 only**, on port **8111** by default. Preferences includes
**Expose localhost transcription API**, a port field and Apply button, the
current status/error, and **Copy token path**. Disabling closes the port and
drains pending requests with 503. A port conflict reports an error in Preferences
and the app log without disrupting dictation; choose another port and Apply.

Files:

- `~/Library/Application Support/ScribeKit/stt_server.token`: persistent random
  bearer token, mode 0600. Existing tokens are reused. Symlinks are refused.
- `~/Library/Application Support/ScribeKit/stt_server.port`: the actual port,
  atomically written after successful bind, including when configured port is 0.

The ScribeKit spelling in these API paths matches the consumer contract. Existing
AudioWhisper settings, history, Python runtime, and model cache paths stay intact.
The runtime adds PyAV 16.1 for bundled FFmpeg decoding; the dependency lock retains
all existing package versions. The installed runtime gains that dependency when
the updated app performs its normal runtime sync.

Hermes / OpenAI audio client settings:

- Base URL: `http://127.0.0.1:8111/v1` (substitute the selected port).
- API key: contents of the token file, without its trailing newline.
- Model: `parakeet-tdt-0.6b-v2` or `scribekit-parakeet`.

Opening the listener does not load Parakeet. A fresh app process can report
`model_loaded: false` until dictation or normal app setup/warmup loads it. HTTP
transcription then returns 503 with `Retry-After: 1`. If the app is closed, or the
API is disabled, clients get connection refused. These are expected states.

## Endpoints

`POST /v1/audio/transcriptions` requires bearer authentication and multipart form
data. Content-Length and chunked request bodies are supported, including
`Expect: 100-continue`. Fields:

| Field | Behavior |
| --- | --- |
| `file` | Required single audio file. WAV, MP3, M4A/AAC, FLAC, Ogg/Opus, WebM. CPU decoding to mono Float32 at 16 kHz. |
| `model` | Optional canonical ID or alias above. Unknown IDs return 400, `invalid_request_error`, `model_not_found`, message `unknown model`. |
| `language` | Accepted; the resident v2 model is English. |
| `response_format` | `json` (default) or `verbose_json`. Both return `{"text":"..."}` for now, including empty recognized text. |

Limits: 100 MiB for the entire HTTP body; at least 0.1 seconds of decoded audio;
at most 2 hours. Duration is rejected before decoding when metadata is available,
and always enforced against the actual resampled sample count before inference.
Unknown/unreliable metadata never permits unbounded decoding. Uploads spool to
private temporary files; inference holds only one short audio chunk at a time.
HTTP headers and text form fields are bounded independently. Invalid audio is 400,
oversized bodies are 413, full admission is 429. Uploads must finish within 120 s.
FFmpeg demuxers and protocols are restricted to the supported local media formats;
uploaded playlists cannot make network requests.

Responses include `X-Request-Id` and `X-ScribeKit-Queue` (integer milliseconds spent
waiting for the inference worker, summed across chunks). 429 and 503 include
`Retry-After: 1`. Responses are not cached, and each connection closes after one
response. Audio bytes, filenames, token contents, and API transcripts are not logged.

`GET /v1/models` requires the token. It returns the OpenAI list envelope containing
exactly `parakeet-tdt-0.6b-v2` when resident, or an empty data array when unloaded.

`GET /healthz` is unauthenticated and performs no inference. It returns `status`,
`model_loaded`, `uptime_s`, and the app `version`.

Browser origins default to `http://127.0.0.1:*` and `http://localhost:*`. Responses
reflect only an allowed origin, never `Access-Control-Allow-Origin: *`. Preflight
OPTIONS is unauthenticated; the actual `/v1` request always needs a token. Set the
`localhostSTTOrigins` array in the app's existing UserDefaults domain to restrict
this further (for example, just `http://localhost:3000`, or an empty array to
disable browser origins), then Apply or relaunch. Only these loopback HTTP hosts
are accepted in configuration; there is no LAN option.

## Dictation priority and cancellation

A single dedicated inference worker executes **all** model setup/warmup, live
preview, final dictation, and HTTP chunks. The stdin reader keeps receiving app
requests while a chunk executes. Dictation uses a separate high-priority deque
and always runs ahead of queued HTTP work. Capturing microphone audio does not
wait for this worker. Recording state blocks HTTP inference for the recording's
duration even with live preview off, and is restored on daemon restart.

HTTP chunks start only after 500 ms with no dictation work in flight, and while
the app is not recording. Audio is split into 20–25 s windows, preferring a quiet
40 ms frame near the boundary; otherwise adjacent windows overlap by 0.5 s and
matching boundary words are merged. Each chunk is separately scheduled, allowing
dictation to run between chunks. The model's normal decoding settings are used.

**An already-running API chunk cannot be preempted.** Dictation inference can wait
for that chunk to finish, then gets priority over every queued API chunk. This
implements the stated chunk-boundary QoS contract; it is not a promise of zero
additional inference latency. The added latency is the remaining compute time
of one chunk, not the 20–25 s audio duration. Real hardware latency needs live
measurement. Chunk joining may still repeat or lose a boundary word in difficult
speech; there is no streaming/timestamp API in this version.

At most **four API jobs total**, including uploads, decoding, waiting, and the
active job, are admitted. Excess requests get 429 before their bodies are read.
There is also a 128-connection header/idle cap and a 10 s header timeout. CPU
decoding uses one worker. Client disconnects remove queued inference and clean up
temporary data; an active chunk finishes without scheduling a successor.

The scheduler sleeps on a condition when idle. The only inference scheduling
timer is the remaining 500 ms grace interval when HTTP work is actually waiting.
App shutdown closes the listener and sends 503 independently of a model call.
SIGTERM or stdin EOF never joins pending GPU work. The app's termination reply
also has an 800 ms fallback.

## Validation

Automated tests use real loopback sockets, real PyAV decoding/resampling, and an
injected sentinel model to avoid loading another model alongside a running app:

```sh
python -m unittest discover -s Tests -p 'test_stt_server.py' -v
python -m unittest discover -s Tests -p 'test_ml_*.py' -v
swift test --filter 'LocalSTTTests|MLDaemonLifecycleTests|MLDaemonManagerTests|SetupDefaultsTests|StreamingPreviewTests|AudioRecorderTests'
```

Coverage includes all requested formats, a 10 s upload, model identity/no loader
call, auth, model IDs, duration/body limits, chunked uploads, multipart boundaries,
CORS, token permissions, ephemeral ports, occupied ports, disabled listeners,
disconnect cancellation, dictation order and idle grace, and shutdown during an
unfinished chunk. The abuse test submits 100 parallel uploads during a recording
reservation: four are admitted, 96 get 429/Retry-After, dictation executes first,
and the admitted uploads complete after recording ends. A subprocess test runs
the actual RPC main loop while HTTP inference is blocked and checks valid RPC
responses plus SIGTERM/503/listener closure within one second.

Live acceptance still requires an updated app, a real speech sample and a safe
idle restart. Compare physical footprint of the **same daemon PID** immediately
before and after the first API transcription, confirm its existing model object
is reused, and measure the dictation wait when arriving mid-chunk. Do not launch
a second real model beside the installed app just to run this test. The mocked
identity test proves routing; it does not measure real Metal memory growth or
recognition accuracy. Pushing this source does not update the installed app.
