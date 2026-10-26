# Third-party and model notices

The root [MIT License](LICENSE) applies to mdspeak's original source code only.
It does not relicense Python packages, MLX-Audio, model weights, model cards,
or voices downloaded at runtime.

- The supported local baseline calls the separately installed
  [mlx-audio](https://github.com/Blaizzy/mlx-audio) command and downloads the
  configured Kokoro model on first use. Review their upstream licenses and
  model cards before use or redistribution.
- mdspeak does not distribute model weights, reference audio, or a cloned
  voice. Its default bm_fable Kokoro voice is a built-in model voice, not a
  personal reference recording.
- Marvis remains an experimental alternative. IndexTTS-2 and any voice-cloning
  workflow are disabled unless explicitly configured; their terms, model
  provenance, and reference-audio permissions are outside this repository's
  MIT grant.

This is an operational notice, not legal advice.
