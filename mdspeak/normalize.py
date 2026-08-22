"""Markdown -> spoken prose.

The whole point of this module (anchor A7 of the prompt): a TTS engine fed raw
markdown voices the syntax. Hashes become "hash hash", asterisks become
"asterisk", table pipes become "vertical bar". This turns markdown into text a
person would actually say.

Design rules:
- Fenced code is announced, never read. Agent output is mostly prose + diffs;
  hearing 40 lines of TypeScript by ear is useless.
- URLs are dropped, link text is kept. "click here, h t t p s colon slash
  slash" is the single worst thing a reader can do to you.
- Structure becomes sentences. A heading is a sentence; a list item is a
  sentence. Punctuation is what gives the engine its prosody, so we spend it.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------- code blocks

_FENCE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<lang>[^\n]*)\n"
    r"(?P<body>.*?)"
    r"^[ \t]*(?P=fence)[ \t]*$",
    re.DOTALL | re.MULTILINE,
)

# A fence that never closes (truncated agent output) — swallow to end of text.
_FENCE_UNCLOSED = re.compile(r"^[ \t]*(`{3,}|~{3,})([^\n]*)\n(.*)\Z", re.DOTALL | re.MULTILINE)

_LANG_NAMES = {
    "js": "JavaScript",
    "jsx": "JavaScript",
    "javascript": "JavaScript",
    "ts": "TypeScript",
    "tsx": "TypeScript",
    "typescript": "TypeScript",
    "py": "Python",
    "rb": "Ruby",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
    "console": "shell",
    "yml": "YAML",
    "yaml": "YAML",
    "json": "JSON",
    "md": "markdown",
    "sql": "SQL",
    "html": "HTML",
    "css": "CSS",
    "diff": "diff",
    "php": "PHP",
    "go": "Go",
    "rs": "Rust",
    "": "",
}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _announce_code(lang: str, body: str) -> str:
    lines = [ln for ln in body.splitlines() if ln.strip()]
    key = lang.strip().lower().split()[0] if lang.strip() else ""
    name = _LANG_NAMES.get(key, key.replace("-", " ").title())
    label = f"{name} code block" if name else "code block"
    return f"{label}, {_plural(len(lines), 'line')}."


# ---------------------------------------------------------------- inline

_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"(?<!!)\[(?P<text>[^\]]*)\]\((?P<url>[^)]*)\)")
_REF_LINK = re.compile(r"(?<!!)\[(?P<text>[^\]]+)\]\[[^\]]*\]")
_REF_DEF = re.compile(r"^[ \t]*\[[^\]]+\]:[ \t]*\S+.*$", re.MULTILINE)
_AUTOLINK = re.compile(r"<(?P<url>[a-z][a-z0-9+.-]*://[^>\s]+|[^@>\s]+@[^@>\s]+)>", re.I)
_BARE_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>()\[\]]+", re.I)
_BARE_WWW = re.compile(r"\bwww\.[^\s<>()\[\]]+", re.I)
_INLINE_CODE = re.compile(r"(?P<ticks>`+)(?P<code>.+?)(?P=ticks)", re.DOTALL)
_HTML_TAG = re.compile(r"</?[a-z][a-z0-9-]*(?:\s[^<>]*)?/?>", re.I)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BOLD_ITALIC = re.compile(r"(\*{1,3}|_{1,3})(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)
_FOOTNOTE_REF = re.compile(r"\[\^[^\]]+\]")

_DOMAIN_WORDS = {"github.com": "GitHub", "gitlab.com": "GitLab", "stackoverflow.com": "Stack Overflow"}


def _url_to_words(url: str) -> str:
    """A URL with no link text becomes its host, spoken as a name."""
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url, flags=re.I).split("/")[0].split("?")[0]
    host = host.removeprefix("www.").lower()
    if host in _DOMAIN_WORDS:
        return f"a link to {_DOMAIN_WORDS[host]}"
    if not host or "@" in host:
        return "a link"
    # example.com -> "example dot com" reads worse than "example"; keep the name only.
    return f"a link to {host.rsplit('.', 1)[0].replace('-', ' ').replace('.', ' ')}"


def _strip_inline(text: str) -> str:
    text = _HTML_COMMENT.sub(" ", text)
    text = _IMAGE.sub(lambda m: f"image, {m['alt']}." if m["alt"].strip() else "", text)
    # Link text carries the meaning; the URL never does when spoken.
    text = _LINK.sub(lambda m: m["text"].strip() or _url_to_words(m["url"]), text)
    text = _REF_LINK.sub(lambda m: m["text"], text)
    text = _FOOTNOTE_REF.sub("", text)
    text = _AUTOLINK.sub(lambda m: _url_to_words(m["url"]), text)
    text = _BARE_URL.sub(lambda m: _url_to_words(m.group(0)), text)
    text = _BARE_WWW.sub(lambda m: _url_to_words(m.group(0)), text)
    text = _INLINE_CODE.sub(lambda m: m["code"].strip(), text)
    text = _HTML_TAG.sub(" ", text)
    text = _STRIKE.sub(r"\1", text)
    text = _BOLD_ITALIC.sub(r"\2", text)
    return text


# ---------------------------------------------------------------- block level

_HEADING = re.compile(r"^[ \t]*(#{1,6})[ \t]*(?P<text>.*?)[ \t]*#*[ \t]*$")
_SETEXT = re.compile(r"^[ \t]*(=+|-{2,})[ \t]*$")
_HRULE = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_BULLET = re.compile(r"^(?P<indent>[ \t]*)[-*+][ \t]+(?P<text>.*)$")
_ORDERED = re.compile(r"^(?P<indent>[ \t]*)(?P<num>\d{1,3})[.)][ \t]+(?P<text>.*)$")
_QUOTE = re.compile(r"^[ \t]*>[ \t]?(?P<text>.*)$")
_TASK = re.compile(r"^\[(?P<mark>[ xX~-])\][ \t]+(?P<text>.*)$")
_TABLE_SEP = re.compile(r"^[ \t]*\|?[ \t]*:?-{1,}:?[ \t]*(\|[ \t]*:?-{1,}:?[ \t]*)+\|?[ \t]*$")
_TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _sentence(text: str) -> str:
    """End a fragment so the engine gives it a falling intonation."""
    text = text.strip()
    if not text:
        return ""
    return text if text[-1] in ".!?:;," else text + "."


def _emit_table(rows: list[list[str]]) -> list[str]:
    """A table read as "header: value" pairs is followable by ear; pipes are not."""
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    if not body:
        return [_sentence(", ".join(c for c in header if c))]
    out = [f"Table with {_plural(len(body), 'row')}."]
    for row in body:
        pairs = [
            f"{h}: {v}" for h, v in zip(header, row) if v and v not in {"-", "—"}
        ] or [v for v in row if v]
        if pairs:
            out.append(_sentence(", ".join(pairs)))
    return out


# ---------------------------------------------------------------- symbols

_SYMBOL_WORDS = {
    # ponytail: affirmatives go silent, negations do not. A dropped ✅ loses
    # emphasis next to a word that already says "done"; a dropped ❌ inverts
    # the meaning of the sentence. Asymmetry is deliberate.
    "✅": "", "✔": "", "☑": "",
    "❌": "no", "✗": "no", "✘": "no",
    "⚠": "warning", "→": "to", "←": "from", "⇒": "to",
    "≥": "at least", "≤": "at most", "≠": "not equal to", "×": "times",
    "…": "...", "–": "-", "—": "-", "•": "", "·": "",
    "“": '"', "”": '"', "‘": "'", "’": "'", "`": "", "™": "", "®": "", "©": "",
}


def _despan_symbols(text: str) -> str:
    out = []
    for ch in text:
        if ch in _SYMBOL_WORDS:
            word = _SYMBOL_WORDS[ch]
            out.append(f" {word} " if word else " ")
        elif unicodedata.category(ch) in {"So", "Sk", "Cf"}:
            out.append(" ")  # emoji and modifiers: silence beats "sparkles"
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------- whitespace

_HASH_NUM = re.compile(r"#(?=\d)")
_TILDE_NUM = re.compile(r"~(?=\s?\d)")
_TILDE_HOME = re.compile(r"~(?=/)")
_LEFTOVER = re.compile(r"[#*`|~]")


def _speak_specials(text: str) -> str:
    """Give the remaining markdown-ish characters a spoken form, or none.

    These characters reach here for two reasons: a construct this module does
    not parse, or ordinary prose that happens to contain one — "issue #42",
    "~5 seconds", "~/.claude". Refusing to read the text over a stray tilde is
    worse than either, so every survivor gets a reading or gets dropped. What
    must never happen is one being voiced as "hash" or "tilde".
    """
    text = _HASH_NUM.sub("number ", text)
    text = _TILDE_NUM.sub("about ", text)
    text = _TILDE_HOME.sub("home", text)
    return _LEFTOVER.sub(" ", text)


_MULTISPACE = re.compile(r"[ \t]{2,}")
_ORPHAN_PUNCT = re.compile(r"(?<=[.!?:;,])[ \t]*(?=[.!?:;,])")
_BLANKS = re.compile(r"\n{3,}")


def _tidy(text: str) -> str:
    text = _MULTISPACE.sub(" ", text)
    text = _ORPHAN_PUNCT.sub("", text)
    lines = [ln.strip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = _BLANKS.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------- public

def normalize(md: str, *, read_code: bool = False) -> str:
    """Turn markdown into prose a TTS engine can read without voicing syntax.

    read_code=True reads fenced code contents aloud instead of announcing them.
    """
    if not md or not md.strip():
        return ""

    md = md.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)

    # Strip YAML front matter — metadata, never spoken.
    if md.startswith("---\n"):
        end = md.find("\n---", 4)
        if end != -1:
            md = md[end + 4 :]

    # Fenced code out first, before any inline rule can chew on its contents.
    blocks: list[str] = []

    def _stash(m: re.Match) -> str:
        body, lang = m["body"], m["lang"]
        blocks.append(body if read_code else _announce_code(lang, body))
        return f"\n\x00{len(blocks) - 1}\x00\n"

    md = _FENCE.sub(_stash, md)
    md = _FENCE_UNCLOSED.sub(
        lambda m: _stash_unclosed(m, blocks, read_code), md
    )
    md = _REF_DEF.sub("", md)

    out: list[str] = []
    table: list[list[str]] = []
    # A markdown paragraph is many source lines but one spoken sentence group.
    # Inline stripping and sentence-ending happen on the joined block, never on
    # a single wrapped line — otherwise every line break becomes a full stop.
    para: list[str] = []

    def flush_para() -> None:
        if not para:
            return
        text = _sentence(_strip_inline(" ".join(para)))
        para.clear()
        if text:
            out.append(text)

    def blank() -> None:
        flush_para()
        if out and out[-1] != "":
            out.append("")

    for raw in md.split("\n"):
        line = raw.strip()

        # --- table accumulation
        if _TABLE_ROW.match(raw) or (table and "|" in raw and line):
            flush_para()
            if not _TABLE_SEP.match(raw):
                table.append(_cells(raw))
            continue
        if table:
            out.extend(_emit_table(table))
            table = []

        if not line:
            blank()
            continue

        # --- fenced code placeholder
        if line.startswith("\x00") and line.endswith("\x00"):
            blank()
            out.append(blocks[int(line.strip("\x00"))])
            out.append("")
            continue

        if _HRULE.match(raw):
            blank()
            continue

        # --- setext heading: the text is the line already buffered
        if _SETEXT.match(raw) and para:
            flush_para()
            out.append("")
            continue

        if m := _HEADING.match(raw):
            blank()
            text = _sentence(_strip_inline(m["text"]))
            if text:
                out.append(text)
                out.append("")
            continue

        if m := _QUOTE.match(raw):
            raw = m["text"]
            line = raw.strip()
            if not line:
                blank()
                continue

        # --- list items each start their own sentence
        if m := _BULLET.match(raw):
            flush_para()
            body = m["text"]
        elif m := _ORDERED.match(raw):
            flush_para()
            body = m["text"]
        else:
            body = line

        if t := _TASK.match(body.strip()):
            body = ("Done: " if t["mark"] in "xX" else "To do: ") + t["text"]

        para.append(body.strip())

    flush_para()
    if table:
        out.extend(_emit_table(table))

    return _tidy(_speak_specials(_despan_symbols("\n".join(out))))


def _stash_unclosed(m: re.Match, blocks: list[str], read_code: bool) -> str:
    body = m.group(3)
    blocks.append(body if read_code else _announce_code(m.group(2), body))
    return f"\n\x00{len(blocks) - 1}\x00\n"


# Characters that must never survive into spoken text (anchor A7). Every one of
# these is handled by `_speak_specials`, so a hit here is a bug in this module —
# never something the user's input can cause.
FORBIDDEN = "#*`|~"


def leaked_syntax(text: str) -> list[str]:
    """Return the markdown syntax that survived normalization. Empty == clean."""
    bad = [c for c in FORBIDDEN if c in text]
    if re.search(r"\]\(", text):
        bad.append("](")
    if re.search(r"[a-z][a-z0-9+.-]*://", text, re.I):
        bad.append("://")
    if "\x00" in text:
        bad.append("placeholder")
    return bad
