# Contributing

## Running it

```bash
pip install -e ".[dev]"
python -X utf8 -m pytest tests -q
```

No agent CLI is needed to run the suite. `FakeDriver` posts to the board exactly
as a real seat does, so turn-taking, the caps and the human gate are exercised
without spending a token. `-X utf8` matters on Windows, where the console
codepage is not UTF-8 and council traffic frequently is not English.

## What a good test looks like here

**Assert on computed board state, not on the shape of the code.** A test that
greps for a guard clause goes green while the guard is bypassed somewhere else. A
test that runs the loop and counts the turns does not. Several bugs in this
repository's history were live while a shape-check passed.

The UI surfaces have headless harnesses — Textual's pilot and prompt_toolkit's
pipe input — so there is no excuse for shipping a screen nobody has executed.
That happened once here, and `tests/test_tui.py` and `tests/test_console.py` are
the answer to it.

## Adding a CLI adapter

Read `docs/DRIVERS.md` first. Every row in it was measured against the binary,
not read from documentation, and the four traps recorded there each presented as
a different problem than they were.

An adapter is about forty lines: build argv, say whether the prompt goes on stdin,
say which efforts the CLI actually accepts, and narrow the tool surface. It does
**not** carry the agent's reply back — the agent posts to the board itself, which
is why five CLIs with five output formats need no output parsers.

Then prove it: `mooting doctor --only <seat>` spends one real turn and asserts on
what landed on the board. A CLI can start, load the MCP server, decline to call
it and exit 0; a return-code check goes green while the seat is mute.

## Changing what a seat may do

The blast radius lives in one method per adapter (`tool_profile`), except Codex,
whose containment is *where* it runs. Execution needs two independent keys — a
seat registered `--capability execute` and a wake for an approved task on a work
topic — and `Store.decide` is the only code path that moves a task out of
`draft`. Please keep both properties checkable rather than promised in a prompt.

## Reporting a bug

The board is a plain SQLite file. `mooting show <topic>` and the `wakes` table
usually say what happened, and both are safe to paste with the message bodies
trimmed.

## Cutting a release

Pushing a `v*` tag does all of it — matrix tests, build, `twine check --strict`,
publish to PyPI, and the GitHub release with the artifacts attached.

```bash
# 1. bump pyproject.toml, commit
# 2. tag with the same number, or the release refuses before it uploads anything
git tag -a v0.1.2 -m "0.1.2"
git push origin main --follow-tags
```

The tag and `pyproject.toml` must agree; the first job compares them and stops
if they do not. PyPI will not let a version be replaced, so a mismatched tag is
a permanent mistake rather than a retry.

`workflow_dispatch` runs the same thing with `dry_run` on by default: everything
up to publishing, so the build and the checks can be exercised without spending
a version number.

**One-time PyPI setup.** Publishing uses Trusted Publishing, so there is no
long-lived token in the repository. On
<https://pypi.org/manage/project/mooting/settings/publishing/>, add a publisher:

| field | value |
|---|---|
| Owner | `jyunming` |
| Repository | `mooting` |
| Workflow name | `release.yml` |
| Environment | `pypi` |

Until that exists the publish step fails with an OIDC error and nothing is
uploaded; the build and the tests still run.
