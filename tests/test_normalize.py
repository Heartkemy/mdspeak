"""Tests for the markdown -> prose normalizer (prompt anchor A7).

The hard requirement is negative: no markdown syntax may survive into the text
handed to the TTS engine. `leaked_syntax` is the gate; the rest of these tests
pin down that the *meaning* survives too, since deleting everything would also
pass the gate.
"""

import pytest

from mdspeak.normalize import leaked_syntax, normalize

AGENT_OUTPUT = """\
# Refactor: auth middleware

I touched **three** files. The root cause was in `validateToken()` — it used
`<` instead of `<=`, so a token expiring *exactly* on the boundary was
rejected.

## Changes

- `src/auth/middleware.ts` — fixed the comparison
- `src/auth/session.ts` — moved behind an interface so tests can fake it
- [the login route](https://github.com/acme/app/blob/main/src/routes/login.ts)
  now returns a structured error

### Before

```ts
if (token.exp < Date.now()) {
  throw new Error("expired");
}
```

### After

```typescript
if (token.exp <= Date.now()) {
  throw new AuthError({ code: "token_expired" });
}
```

| File | Change | Tested |
|------|--------|--------|
| middleware.ts | comparison | yes |
| session.ts | interface | yes |

> Note: this does not change the token TTL.

See https://example.com/docs for the rest. ✅ Done.

---

1. Run the tests
2. Ship it
"""


def test_no_syntax_leaks_on_realistic_agent_output():
    spoken = normalize(AGENT_OUTPUT)
    assert leaked_syntax(spoken) == [], spoken


def test_meaning_survives():
    spoken = normalize(AGENT_OUTPUT)
    assert "Refactor: auth middleware." in spoken
    assert "three files" in spoken
    assert "validateToken()" in spoken
    assert "the login route" in spoken  # link text kept
    assert "acme" not in spoken  # URL dropped
    assert "File: middleware.ts, Change: comparison" in spoken  # table read as pairs
    assert "this does not change the token TTL" in spoken  # blockquote unwrapped


def test_code_blocks_are_announced_not_read():
    spoken = normalize(AGENT_OUTPUT)
    assert "TypeScript code block, 3 lines." in spoken
    assert "Date.now" not in spoken
    assert "AuthError" not in spoken


def test_code_blocks_can_be_read_when_asked():
    spoken = normalize(AGENT_OUTPUT, read_code=True)
    assert "AuthError" in spoken


@pytest.mark.parametrize(
    "md",
    [
        "# H1\n## H2\n### H3",
        "**bold** *italic* ***both*** _under_ __strong__ ~~struck~~",
        "`inline` and ``double tick`` and ```triple```",
        "[text](https://a.b/c) ![alt](img.png) <https://x.y> https://z.w/q",
        "| a | b |\n|---|---|\n| 1 | 2 |",
        "- one\n- two\n  - nested\n1. first\n2. second",
        "> quoted\n>> deeper",
        "***\n---\n___",
        "- [x] done thing\n- [ ] pending thing",
        "```\nunfenced lang\n```",
        "```python\nprint(1)\n",  # unclosed fence, truncated output
        "text with ✅ ❌ ⚠ → ≥ emoji 🎉 and “smart quotes”",
        "<div class='x'>html</div><!-- comment -->",
        "---\ntitle: front matter\n---\n\nBody text.",
        "[ref][1]\n\n[1]: https://example.com",
        "A footnote[^1].\n\n[^1]: the note",
    ],
)
def test_no_syntax_leaks_across_constructs(md):
    spoken = normalize(md)
    assert leaked_syntax(spoken) == [], f"{md!r} -> {spoken!r}"


@pytest.mark.parametrize(
    "md, expect",
    [
        ("Fixed in issue #42 today.", "issue number 42"),
        ("It takes ~5 seconds.", "about 5 seconds"),
        ("Skills live in ~/.claude/skills.", "home/.claude/skills"),
        ("The glob is *.ts and a|b.", "a b"),
        ("Approx ~ 30 requests.", "about 30 requests"),
    ],
)
def test_prose_characters_are_spoken_not_rejected(md, expect):
    """A stray ~ or # in normal prose must never block the read (regression)."""
    spoken = normalize(md)
    assert leaked_syntax(spoken) == [], spoken
    assert expect in spoken


def test_tilde_in_prose_does_not_raise_through_the_api():
    from mdspeak.server import app
    from fastapi.testclient import TestClient

    r = TestClient(app, base_url="http://127.0.0.1").post(
        "/api/preview", json={"markdown": "Run it in ~/.claude — takes ~5 s."}
    ).json()
    assert r["leaks"] == []


def test_empty_and_whitespace():
    assert normalize("") == ""
    assert normalize("   \n\n  ") == ""


def test_task_lists_read_as_status():
    spoken = normalize("- [x] ship it\n- [ ] write docs")
    assert "Done: ship it." in spoken
    assert "To do: write docs." in spoken


def test_bare_url_becomes_a_name_not_a_spelling():
    spoken = normalize("Docs live at https://github.com/acme/repo now.")
    assert "GitHub" in spoken
    assert "acme" not in spoken and "://" not in spoken


def test_headings_become_sentences():
    spoken = normalize("## Summary\nIt works.")
    assert spoken.startswith("Summary.")


def test_prose_paragraph_stays_one_line():
    spoken = normalize("This is one\nparagraph across\nthree source lines.")
    assert spoken.count("\n") == 0


def test_table_without_body_rows():
    spoken = normalize("| a | b |\n|---|---|")
    assert leaked_syntax(spoken) == []
    assert "a, b" in spoken


def test_long_code_block_line_count():
    md = "```js\n" + "\n".join(f"line{i};" for i in range(12)) + "\n```"
    assert "JavaScript code block, 12 lines." in normalize(md)


def test_single_line_code_block_is_singular():
    assert "code block, 1 line." in normalize("```\nonly;\n```")
