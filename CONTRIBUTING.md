# Contributing

Keep changes small, testable, and free of personal paths, credentials, model
weights, or reference audio.

~~~sh
uv sync --locked --extra dev
uv run pytest
swift build --package-path native/SpeakSelection
~~~

The Python suite is model-free by default. Do not add a test that downloads
weights, calls a hosted provider, or depends on local macOS permissions.

In a macOS GUI session, `bash native/SpeakSelection/test.sh` checks provider
preferences and the player's startup observer with synthetic state. It saves
and restores the test process's preferences.

Use a focused branch and Conventional Commit message. For behavior changes,
include a synthetic regression test. Open a GitHub issue before proposing a
larger design or runtime change.
