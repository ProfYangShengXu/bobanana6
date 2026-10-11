"""Refuse an inline interpreter script in ``shell.run`` -- make it a file first.

Why this exists (measured, not a style preference): a block-level worker rewrote
the whole multi-thousand-character program every turn instead of editing the
previous one, because the program never landed on disk -- 14 of 32 ``shell.run``
calls in one measured run were ``python3 << 'EOF'`` heredocs. Nothing on disk
means nothing to patch, so ``files.patch`` was never called and every turn paid
for a full rewrite.

The rule is deliberately **high precision / low recall** (see
``改动文档-守卫-20261007`` §2): only an *unambiguous* inline script is refused.
``ls`` / ``cat`` / ``python3 script.py`` / a short ``python3 -c "print(1+1)"``
run untouched. A refusal names ``violated`` / ``expected`` / ``got`` and points
at the fix. It refuses **that one call** -- the registry turns the raised error
into a failed ``ToolResult``, so the block keeps going, it is never judged dead.

Pure and dependency-free: no model, no I/O, no domain vocabulary. The interpreter
names and flag names below are language runtimes, not task concepts.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence

__all__ = [
    "INLINE_CODE_MAX_CHARS",
    "inline_script_problem",
]

#: A ``-c`` / ``-e`` / ``-r`` body shorter than this is treated as a one-liner
#: and let through. Why 200: the measured problem scripts were 3k-11k characters
#: (7,385 longest before the change, 10,876 after), so 200 sits ~20x below the
#: smallest real script while leaving generous room for a genuine one-liner.
#: A body with any newline is a script regardless of length and is refused too.
INLINE_CODE_MAX_CHARS = 200

#: Command separators that end one simple command and begin another. ``>`` / ``<``
#: are *not* here: a redirection belongs to the command it is attached to.
_BOUNDARIES = frozenset({"|", "||", "&", "&&", ";", "(", ")"})

#: Wrappers that may precede the real command word. ``timeout`` matters most:
#: the guidance tells the model about timeouts, so ``timeout 120 python3 <<EOF``
#: is a natural way to reach the very heredoc this guard refuses.
_WRAPPERS = frozenset(
    {
        "env",
        "sudo",
        "doas",
        "nohup",
        "time",
        "timeout",
        "command",
        "builtin",
        "exec",
        "nice",
        "stdbuf",
        "xargs",
    }
)

#: Commands whose heredoc body lands in a file. Writing a script to a file with
#: one of these still inlines the script in the command (measured: a block worker
#: created its generator with ``cat > generate_*.py << 'EOF'`` -- 8 such writes,
#: zero ``files.write`` calls), so it is the same "no file to patch" failure
#: dressed differently.
_WRITE_COMMANDS = frozenset({"cat", "tee"})

#: Only a *script* file is refused -- a heredoc writing data (``> data.txt``,
#: ``> out.csv``) is an ordinary command and stays untouched.
_SCRIPT_SUFFIXES = (
    ".py",
    ".pyw",
    ".sh",
    ".bash",
    ".zsh",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".rb",
    ".pl",
    ".php",
    ".lua",
    ".r",
)

#: ``python`` / ``python3`` / ``python3.12`` -- versioned names, not a fixed list.
_PYTHON_NAME = re.compile(r"^python\d*(?:\.\d+)*$")

#: Other interpreter base names (lower-cased, no path, no ``.exe``).
_OTHER_INTERPRETERS = frozenset(
    {"node", "nodejs", "deno", "bun", "perl", "ruby", "php", "lua", "rscript", "bash", "sh", "zsh", "ksh", "dash"}
)

#: Shells: they take ``-c`` like python does.
_SHELLS = frozenset({"bash", "sh", "zsh", "ksh", "dash"})

#: Per-family flags whose following token is an inline program body.
_INLINE_FLAGS: dict[str, frozenset[str]] = {
    "python": frozenset({"-c"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "nodejs": frozenset({"-e", "--eval", "-p", "--print"}),
    "deno": frozenset({"eval"}),
    "bun": frozenset({"-e", "--eval"}),
    "perl": frozenset({"-e", "-E"}),
    "ruby": frozenset({"-e"}),
    "php": frozenset({"-r"}),
    "lua": frozenset({"-e"}),
    "rscript": frozenset({"-e"}),
    "shell": frozenset({"-c"}),
}

#: ``<<TAG`` / ``<<-TAG`` / ``<<'TAG'`` / ``<<"TAG"`` on a source line.
_HEREDOC_MARK = re.compile(r"<<(?P<dash>-?)\s*(?P<quote>['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)")


def _base_name(token: str) -> str:
    """``/usr/bin/python3`` -> ``python3``; ``PY.EXE`` -> ``py``."""
    name = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    return name.removesuffix(".exe")


def _family(name: str) -> str:
    """The inline-flag family of an interpreter base name (``""`` = not one)."""
    if _PYTHON_NAME.match(name):
        return "python"
    if name in _SHELLS:
        return "shell"
    return name if name in _INLINE_FLAGS else ""


def _is_interpreter(name: str) -> bool:
    return bool(_family(name))


def _strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc bodies, keeping the marker line that declares them.

    Heredoc bodies are *data*, not code: a ``cat <<EOF > data.txt`` body must not
    be scanned for interpreter calls, while the ``python3 <<EOF`` marker line must
    still be refused. This only removes the body lines -- it never edits code.
    """
    lines = command.split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        tags = [match.group("tag") for match in _HEREDOC_MARK.finditer(line)]
        index += 1
        for tag in tags:
            while index < len(lines) and lines[index].strip() != tag:
                index += 1
            index += 1  # step past the terminator line
    return "\n".join(kept)


def _tokens(code: str) -> list[str] | None:
    """Shell tokens, or ``None`` when the command cannot be parsed.

    ``None`` means **fail open**: an unparsable command is not confidently an
    inline script, and a false refusal costs more than a miss (守卫纪律一).
    """
    lexer = shlex.shlex(code, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""  # '#' is code inside a body, never a shell comment here
    try:
        return list(lexer)
    except ValueError:
        return None


def _segments(tokens: Sequence[str]) -> list[tuple[list[str], bool]]:
    """Split into simple commands, remembering which ones are fed by a pipe."""
    segments: list[tuple[list[str], bool]] = []
    current: list[str] = []
    piped = False
    for token in tokens:
        if token in _BOUNDARIES:
            if current:
                segments.append((current, piped))
            current = []
            piped = token == "|"
            continue
        current.append(token)
    if current:
        segments.append((current, piped))
    return segments


def _command_head(segment: Sequence[str]) -> int:
    """Index of the command word, or ``-1`` when the segment does not start one.

    Only a leading command counts: ``echo python3 -c "..."`` is *not* an
    interpreter invocation, so it is never refused. A leading launcher is
    stripped first -- its options and one bare positional argument
    (``timeout 120 python3``, ``sudo -u root python3``, ``nice -n 10 python3``)
    -- otherwise wrapping the heredoc would walk straight past the guard.
    """
    index = 0
    positional_left = 0
    saw_wrapper = False
    while index < len(segment):
        token = segment[index]
        if "=" in token and not token.startswith("-"):
            index += 1  # an environment assignment: PYTHONPATH=src python3 ...
            continue
        name = _base_name(token)
        if name in _WRAPPERS:
            saw_wrapper = True
            positional_left = 1  # a wrapper may take one bare positional (timeout 120)
            index += 1
            continue
        if _is_interpreter(name):
            return index
        if saw_wrapper and token.startswith("-"):
            index += 1  # the wrapper's own option
            continue
        if saw_wrapper and positional_left > 0:
            positional_left -= 1
            index += 1  # the wrapper's own argument (or its option's value)
            continue
        return -1
    return -1


def _is_heredoc_token(token: str) -> bool:
    return token.startswith("<<")


def _heredoc_into_a_script_file(segment: Sequence[str]) -> bool:
    """``cat > gen.py << 'EOF'`` / ``tee gen.py <<EOF`` -- the script is a file,
    but it was still inlined in the command."""
    index = 0
    while index < len(segment) and "=" in segment[index] and not segment[index].startswith("-"):
        index += 1  # env assignments
    if index >= len(segment) or _base_name(segment[index]) not in _WRITE_COMMANDS:
        return False
    rest = segment[index + 1 :]
    if not any(_is_heredoc_token(token) for token in rest):
        return False
    return any(token.lower().endswith(_SCRIPT_SUFFIXES) for token in rest)


def _flag_like(token: str) -> bool:
    return token == "-" or token.startswith("-")


def _inline_body(interpreter: str, args: Sequence[str]) -> tuple[str, str] | None:
    """``(flag, body)`` when an inline-program flag is used, else ``None``.

    ``--eval=code`` carries its body in the same token; ``-c code`` in the next.
    An unterminated flag (``python3 -c``) has an empty body and is not refused.
    """
    flags = _INLINE_FLAGS.get(_family(interpreter), frozenset())
    for index, token in enumerate(args):
        name, sep, attached = token.partition("=")
        if name.lower() not in flags:
            continue
        if sep:
            return name, attached
        body = args[index + 1] if index + 1 < len(args) else ""
        return name, body
    return None


def inline_script_problem(command: str) -> str:
    """Why this command is an inline script (``""`` = it is not; let it run).

    Three unambiguous shapes are refused:

    1. a heredoc fed to an interpreter -- ``python3 << 'EOF'`` (also when wrapped:
       ``timeout 120 python3 << 'EOF'``);
    2. a bare interpreter reading its program from a pipe -- ``... | python3``;
    3. an inline body handed to ``-c`` / ``-e`` / ``-r`` etc. that is multi-line
       or at least :data:`INLINE_CODE_MAX_CHARS` characters long.

    Plus one that lands the script in a file without ever becoming editable: a
    heredoc written straight to a script file -- ``cat > gen.py << 'EOF'``.

    Everything else passes: ordinary commands, ``python3 script.py``, short
    one-liners, and a heredoc that writes a *data* file (``cat <<EOF > data.txt``).
    """
    if not isinstance(command, str) or not command.strip():
        return ""

    code = _strip_heredoc_bodies(command)
    tokens = _tokens(code)
    if tokens is None:
        return ""

    for segment, piped in _segments(tokens):
        # 0) the script is inlined *into a file* -- still nothing to patch later.
        if _heredoc_into_a_script_file(segment):
            return _refuse(
                "a script body was written straight to a script file through a heredoc", command
            )

        head = _command_head(segment)
        if head < 0:
            continue
        interpreter = _base_name(segment[head])
        args = segment[head + 1 :]

        # 1) heredoc fed straight to the interpreter: the script is in the command.
        heredoc = False
        for token in args:
            if token in _BOUNDARIES:
                break
            if _is_heredoc_token(token):
                heredoc = True
                break
            if not _flag_like(token):
                break  # a real script argument (python3 script.py <<EOF) -- let it run
        if heredoc:
            return _refuse("a script body was fed to the interpreter through a heredoc", command)

        # 2) a bare interpreter reading its program from a pipe.
        if piped and not args:
            return _refuse("the interpreter was handed its program on stdin through a pipe", command)

        # 3) an inline program body that is multi-line or long.
        found = _inline_body(interpreter, args)
        if found is not None:
            flag, body = found
            if "\n" in body or len(body) >= INLINE_CODE_MAX_CHARS:
                return _refuse(f"a {len(body)}-character program body was inlined with `{flag}`", command)

    return ""


def _refuse(violated: str, command: str) -> str:
    """The named refusal: violated / expected / got, plus the way out."""
    got = command.strip().replace("\n", " ")[:80]
    return (
        "shell.run refused an inline script "
        f"(violated: {violated}; "
        "expected: the script is a file first -- write it with `files.write`, "
        "then run it as `python3 <file>`; "
        f"got: {got!r}). "
        "Next: put the script in a file, then change the few wrong lines with "
        "`files.patch` instead of rewriting the whole thing."
    )
