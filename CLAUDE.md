# CLAUDE.md

Guidance for Claude Code working in this repository.

## Style

Keep it simple.

**Docstrings.** A one-liner wherever one will do. Terse, but written for a
person to read: plain words, no jargon, no filler.

**Comments.** Only to explain a *why* that the code cannot show. Think twice
before adding one at all — prefer code that says it itself. Anything you do add
follows the docstring rules above.

**Private names.** A leading underscore is for tiny helpers. Anything larger is
a normal function.

**Global constants.** Do not add one unless it is actually needed. A value used
in a single place is a local.
