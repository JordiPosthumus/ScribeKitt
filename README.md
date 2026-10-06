# ScribeKitt

<p align="center"><img src="ScribeKittIcon.png" width="128" alt="ScribeKitt app icon"></p>

**Your voice. Your Mac. Your words.**

Local macOS dictation with a KITT-inspired voice display: watch your words appear while you speak, then paste with ⌘V.

<p align="center"><img src="docs/images/showcase.png" width="960" alt="Native macOS dictation panel with a red KITT-style voice display, status indicators, and a live transcript"></p>
<p align="center"><em>The native recorder interface with sample text. Live words appear beside the voice display; the panel expands for longer dictations.</em></p>

**Built on [AudioWhisper by mazdak and contributors](https://github.com/mazdak/AudioWhisper).** The original [MIT license](LICENSE) is preserved. See [Credits](CREDITS.md).

## Daily use

- **Hold the right ⌘ Command key to record; release it to finish.** This is the default on a new installation. The key and behavior can be changed in Settings → Recording.
- A centered floating recorder places a tall KITT-style voice display and decorative status lamps beside the transcript. The text area grows as you speak; earlier words remain visible as the live draft advances.
- Stop recording. The existing full-audio Parakeet pass produces the final text, copies it, and shows a brief animated confirmation that dismisses itself. **Add Trailing Space** is on by default: copied text ending in a full stop, question mark, or exclamation mark gets a trailing space so the next dictation stays separated. You can switch this off in Preferences.
- The completed text is copied to your clipboard. Press **⌘V** yourself wherever you want to paste. **Escape** cancels recording.
- New installations enable live text, microphone boost, completion sounds, and local history kept until you delete it. History, recording preferences, and launch at login live in a compact settings window. Launch at login is opt-in for new users and follows the actual macOS setting.

The default right-⌘ recording key requires **Accessibility** permission; setup guides you through it. The microphone menu remains available without that keyboard permission. The app never sends paste keystrokes. If the key still does not respond after granting permission, **quit ScribeKitt from its menu-bar menu and reopen it**. Turn **Transcription Streaming** off in Preferences to use record-then-transcribe without live audio processing. The setting is saved and applies to the next recording.

## First launch

ScribeKitt requires an **Apple Silicon Mac (M1 or newer)** and macOS 14 or later. The current model supports **English dictation**.

1. Move `ScribeKitt.app` to Applications and open it.
2. Choose **Prepare ScribeKitt**. Setup prepares Python, downloads the roughly 2.5 GB Parakeet v2 model, and checks offline loading. Allow 6 GB of free space and an internet connection for setup.
3. Setup shows **Allow Microphone** and **Allow in Settings** for the recording key. In macOS **Privacy & Security → Accessibility**, enable ScribeKitt. If it is missing, use **+** to add the installed app from Applications.
4. Hold **right ⌘**, speak, release, then paste with **⌘V**. If the key does not respond after granting access, quit ScribeKitt and reopen it.
5. Revisit **Setup & Permissions…** from the microphone menu whenever needed. Start at Login remains an optional setup choice.

Existing installations reuse their model and runtime. If a required microphone or recording-key permission is missing, the permission guide still appears. If a download is interrupted, reopen the app and choose **Prepare ScribeKitt** to retry; completed cached files are reused. Normal model loading and transcription use the local snapshot without online metadata requests.

<details>
<summary>Preview the first-run setup</summary>

<img src="docs/images/setup.png" width="520" alt="ScribeKitt first-run setup with runtime, model download, and offline verification steps">

</details>

## This fork

ScribeKitt focuses on local **Parakeet v2** dictation, live text, and a compact history. File transcription, completion sounds, and microphone boosting are included. It keeps the original project's license and credits.

When upgrading from AudioWhisper or SpeedyWhisper, quit the previous app and replace it. Existing preferences, runtime, and cached models are retained. ScribeKitt uses a dedicated history file at `~/Library/Application Support/AudioWhisper/history.store`; a healthy legacy history is imported automatically with a timestamped backup. The original bundle identifier and support-folder name remain stable. The installed app is `/Applications/ScribeKitt.app`.

## Install the test build

[ScribeKitt 210.21](https://github.com/JordiPosthumus/ScribeKitt/releases/tag/v210.21) is available as a prebuilt test app. No Xcode or Homebrew is needed. Quit any running ScribeKitt/AudioWhisper app, then paste this into Terminal:

```bash
(
  set -e
  installer=$(mktemp -t scribekitt-install)
  trap 'rm -f "$installer"' EXIT
  curl -fsSL https://raw.githubusercontent.com/JordiPosthumus/ScribeKitt/v210.21/scripts/install.sh -o "$installer"
  /bin/bash "$installer" 210.21
)
```

The installer verifies the archive checksum and app signature, backs up an existing ScribeKitt app, installs it, and opens setup. It preserves existing preferences, history, and model files. New defaults apply only where you have not saved a different choice. Start at Login is optional and initially off for new users. The test build is locally signed and has not been Apple-notarized; macOS may require approval in System Settings → Privacy & Security.

Updates are manual for now. Pushing source changes to GitHub does not update an installed app.

## Localhost transcription API

The app can share its resident Parakeet model with Hermes and other OpenAI-compatible
audio clients at `http://127.0.0.1:8111/v1`. It uses the **same cached model in the
app-owned Python daemon**, with no second model load. Preferences → **Expose localhost
transcription API** is enabled by default. The port is configurable; `0` chooses a
free port. Preferences shows the listener status and a **Copy token path** button.

```sh
curl -H "Authorization: Bearer $(cat "$HOME/Library/Application Support/ScribeKit/stt_server.token")" \
  -F file=@sample.wav -F model=scribekit-parakeet \
  http://127.0.0.1:8111/v1/audio/transcriptions
```

The token is generated locally with owner-only permissions. `GET /healthz` needs
no token; all `/v1` data endpoints do. The API never loads a model on demand:
transcription returns **503 until dictation or normal app warmup has loaded it**.
With the app closed or the setting off, the connection is refused. Requests
queued behind dictation wait; excess uploads receive **429** with `Retry-After`.

See [the API contract, scheduling details, and validation](docs/LOCALHOST_STT.md).

## Build

```sh
swift build
swift test
CODE_SIGN_IDENTITY=- scripts/build.sh
```

The release script produces `ScribeKitt.app`. The command above uses ad-hoc signing for local testing; a public download should be signed with Developer ID and notarized before distribution. An installed `uv` executable or a copy in `Sources/Resources/bin/uv` is needed for runtime packaging. The existing Python dependency manifest is deliberately preserved; removing unused packages from the live environment is a separate change.

See [the streaming design and measured validation](docs/STREAMING.md), [the interface notes](SCRIBEKITT.md), [the reduction scope](SLIM_BUILD.md), and [the logo source and generation prompt](docs/branding/logo-prompt.md).
