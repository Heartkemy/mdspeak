# mdspeak

mdspeak is experimental software that reads Markdown as spoken prose. The
supported baseline is local Kokoro with the built-in bm_fable voice; it does not
require a personal reference recording or a hosted account.

| Component | Purpose | Requirements |
| --- | --- | --- |
| Python web/desktop player | Local loopback UI and API | Python 3.10+, macOS, and a separate MLX-Audio runtime for synthesis |
| Python worker (mdspeak-speak) | Local loopback service for the native app | Same Python setup plus the MLX-Audio interpreter |
| Native Speak Selection app | Menu-bar selection reader and audio player | macOS 13+, Xcode Command Line Tools, and manual Accessibility approval |

## Quick start

mdspeak's application dependencies and MLX inference runtime are intentionally
separate: the latter has heavyweight Apple-Silicon packages and model weights.

~~~sh
git clone https://github.com/Heartkemy/mdspeak.git
cd mdspeak
uv sync --locked --extra dev
uv tool install --force mlx-audio --prerelease=allow

# Confirm that the separately installed command is discoverable.
command -v mlx_audio.tts.generate
uv run mdspeak
~~~

Use an Apple-Silicon Mac for MLX synthesis. The first Kokoro request downloads
the configured model and therefore needs network access and local disk space.
If the MLX-Audio command is not on your app's PATH, provide its exact path:

~~~sh
MDSPEAK_MLX_AUDIO_COMMAND="$(command -v mlx_audio.tts.generate)" uv run mdspeak
~~~

The app uses mlx-community/Kokoro-82M-8bit by default. Set
MDSPEAK_KOKORO_MODEL only when you have independently checked the replacement
model's provenance and terms.

### Native menu-bar app

Build the Swift shell after the Python setup:

~~~sh
swift build --package-path native/SpeakSelection
bash native/SpeakSelection/build.sh
open native/SpeakSelection/build/SpeakSelection.app
~~~

The app discovers mdspeak-speak from PATH. For a Finder-launched app with no
suitable PATH, set MDSPEAK_SPEAK_COMMAND or explicitly set the
com.mdspeak.SpeakSelection workerPath preference to an executable path. The
worker normally derives its MLX interpreter from the installed
mlx_audio.tts.generate script; set MDSPEAK_MLX_AUDIO_PYTHON to the runtime's
Python executable only when that derivation cannot work.

Accessibility is a manual macOS privacy permission needed to read selected text
from other apps. The build script uses an existing signing identity only when
one is already available, otherwise it signs ad hoc; it never creates a
certificate or changes privacy settings.

## Local-service trust and data retention

Both services bind to 127.0.0.1, reject non-local Host/origin requests, and
send Cache-Control: no-store. They are **single-user local tools**, not
authenticated network services. Do not run them behind a proxy, tunnel, or
non-loopback listener.

Rendered audio and job files remain under ~/.cache/mdspeak/jobs (web player)
and ~/.cache/mdspeak/speak-jobs (native worker) until you remove them. Treat
that cache as speech-derived local data; deleting ~/.cache/mdspeak removes all
of it. mdspeak has no telemetry in this repository; supported Kokoro requests
remain local after the model download.

## Scope and model terms

Kokoro is the supported public baseline. Marvis is experimental. IndexTTS-2,
custom models, and voice cloning are intentionally unsupported unless you
separately configure them and verify model/reference-audio rights. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md): the repository's MIT license
does not cover dependencies, downloaded model weights, or voices.

No portable performance claim is made here. Latency, memory use, model download
time, and audio quality depend on your hardware, MLX-Audio release, model, and
input. The automated suite uses synthetic text and never loads a neural model.

### Optional cloud engines

Cloudflare Aura 2 and Gemini speech are optional alternatives in the web and
native clients. Kokoro remains the local default. Selecting a cloud provider
sends normalized text to that provider; each client labels that choice.
Configure credentials in an ignored repository-root `.env` when running from
this checkout, or use environment variables with an installed package:

~~~dotenv
CLOUDFLARE_API_TOKEN=your-workers-ai-token
CLOUDFLARE_ACCOUNT_ID=your-account-id
# or GEMINI_API_KEY=your-api-key
~~~

These optional paths have mocked request/WAV and cancellation tests; live model
access, quotas and cloud audio quality have not been verified. Use private text
only when you accept the provider's terms and data handling. See the official
[Gemini speech API](https://ai.google.dev/gemini-api/docs/speech-generation)
and [Cloudflare Aura model](https://developers.cloudflare.com/workers-ai/models/aura-2-en/)
contracts. Their model, account, quota and API terms are separate from this
repository's MIT license. Stopping playback prevents later synthesis chunks;
a provider request already in progress may finish.

## Development

~~~sh
uv sync --locked --extra dev
uv run pytest
swift build --package-path native/SpeakSelection
~~~

The normal test command excludes tests marked slow; do not run a real-model
test unless you deliberately have the required local runtime and model.
Contributions, support, and vulnerability reporting guidance live in
[CONTRIBUTING.md](CONTRIBUTING.md), [SUPPORT.md](SUPPORT.md), and
[SECURITY.md](SECURITY.md).
