# dockerfile-optimizer

A container-optimization micro-agent. It parses a Dockerfile into a structured
model, audits it for caching, size and security problems, and mechanically
rewrites the ones it can fix without guessing.

Requires Python 3.10 or newer. The linting and rewriting core is deterministic,
offline and reproducible; an opt-in advisory layer can consult Gemini about the
judgement calls the core refuses to make. No runtime dependencies on 3.11+; on 3.10 it pulls
in `tomli` alone, because a CI gate should not drag a dependency tree into every
build it guards.

## Install

```bash
pip install -e ".[dev]"   # provides the `stow` command plus the test toolchain
stow --help
```

Running straight from a checkout, without installing:

```bash
./stow --help             # macOS / Linux
.\stow.cmd --help         # Windows (PowerShell or cmd)
python -m app.cli --help  # any platform
```

On Windows, if `stow` or `pytest` is "not recognized" after installing, your
Python `Scripts` directory is not on `PATH`. Use `python -m app.cli` and
`python -m pytest`, or reinstall Python with **Add python.exe to PATH** ticked.

## Commands

```bash
stow analyze <PATH>              # audit; defaults to ./Dockerfile
stow analyze <PATH> --format sarif   # json (default) | text | sarif
stow analyze <PATH> --fail-on high   # low | medium | high | critical
stow analyze <PATH> --disable MISSING_HEALTHCHECK   # repeatable

stow refactor <PATH>             # print the rewrite to stdout
stow refactor <PATH> --write     # rewrite in place
stow refactor <PATH> --check     # exit non-zero if a rewrite would change it

stow suggest <PATH>              # ask a model the calls the engine refuses
stow rules --format text         # the rule catalog
stow version
```

Exit codes: `0` clean, `1` findings at or above the threshold, `2` usage error,
`3` unreadable input. `analyze` and `refactor --check` both drop into CI as gates.

## Rules

Fifteen rules, each with a severity. `--fail-on` decides which ones break a build
(default `medium`, so the `low` ones inform without blocking).

| Severity | Rule | Catches |
| --- | --- | --- |
| critical | `LEAST_PRIVILEGE` | Final stage runs as root, explicitly or by default |
| critical | `SECRET_IN_IMAGE` | A literal secret baked into `ENV`/`ARG` |
| high | `PINNED_VERSION` | Base image on `:latest` or with no tag/digest |
| high | `NO_SUDO` | `sudo` in a build layer |
| high | `ADD_REMOTE` | `ADD` of a URL, fetched without checksum verification |
| medium | `CACHE_CLEANUP` | apt layer that never cleans `/var/lib/apt/lists/*` |
| medium | `APT_UPDATE_ISOLATED` | `apt-get update` alone in a layer, served stale from cache |
| medium | `CACHE_ORDER` | Source copied before dependencies install |
| medium | `LOCKFILE_FALLBACK` | A `\|\|` fallback that discards `--frozen`/`--locked` |
| medium | `PIPE_WITHOUT_PIPEFAIL` | Pipeline that discards an upstream failure |
| low | `ADD_OVER_COPY` | `ADD` where `COPY` is clearer |
| low | `PIP_NO_CACHE` | `pip install` leaving its wheel cache in the layer |
| low | `WORKDIR_ABSOLUTE` | Relative `WORKDIR` |
| low | `MAINTAINER_DEPRECATED` | `MAINTAINER` instead of `LABEL` |
| low | `MISSING_HEALTHCHECK` | Final stage declares no `HEALTHCHECK` |

`LEAST_PRIVILEGE` evaluates **only the final stage**. A `USER` in a builder stage
never reaches the shipped image, so treating it as compliance is a false pass.

## Refactors

`refactor` fires only transforms it can apply mechanically. Anything needing
judgement is left in place and reported on stderr as `# skipped ->`.

| Transform | Effect |
| --- | --- |
| `APT_NO_RECOMMENDS` | Adds `--no-install-recommends` |
| `APT_CACHE_CLEANUP` | Appends list cleanup to the same `RUN`, so it stays one layer |
| `PIP_NO_CACHE` | Adds `--no-cache-dir` |
| `ADD_TO_COPY` | Rewrites `ADD` → `COPY` for plain local paths only |
| `CACHE_ORDER_HOIST` | Moves `COPY . <dest>` below the dependency install |
| `LEAST_PRIVILEGE_USER` / `_DEROOT` | Adds a non-root `USER`, or replaces `USER root` |
| `MULTISTAGE_SPLIT` | Splits a single-stage pip build into `builder` + lean runtime |

What it deliberately will not do:

- **Invent a version tag.** Which release replaces `python:latest` is a human
  call; guessing ships a build that works locally and breaks in production.
- **Rewrite a remote `ADD`.** `COPY` cannot fetch a URL, so the rewrite would
  silently break the build.
- **Split a multi-stage build it cannot rewrite faithfully.** `MULTISTAGE_SPLIT`
  fires on one `FROM` plus one `pip install -r`; other shapes are reported.

Comments, blank-line grouping and hand-written formatting are preserved
verbatim. A comment travels with the instruction it introduces even when a
transform moves that instruction, because a comment is usually the only place
the reasoning exists. A file with nothing to fix comes back byte-identical.

Refactoring is idempotent — running it twice yields the same file, so `--write`
does not churn diffs.

## The advisory layer

`analyze` and `refactor` are deterministic, offline, and reproducible. That is
the property worth protecting, so the model layer is built to sit beside it
rather than inside it.

```bash
export GEMINI_API_KEY=...        # https://aistudio.google.com/apikey
stow suggest Dockerfile
```

`suggest` asks a model **only the questions the engine deliberately refuses** —
which tag should replace `python:latest`, whether a build the engine won't split
could be split by hand. Four properties hold:

| | |
| --- | --- |
| **Opt-in** | `analyze`, `refactor`, `rules` and `version` never open a socket. There is a test that fails if they do. |
| **Additive only** | A model answer that does not respond to a question the engine asked is discarded. It cannot invent a finding. |
| **Never applied** | `suggest` prints. It does not write, and `--write` does not consult it. |
| **Free when quiet** | No open questions means no API call, so a clean Dockerfile costs nothing. |

Output keeps the two sources visually apart, because a reader should never have
to work out which half is reproducible:

```
  ? PINNED_VERSION  python:latest  (line 1)
      The engine will not invent a replacement tag...
      ◐ model (medium): Pin to python:3.12-slim
        because: The build installs pip packages only; slim keeps the image small.

Nothing above has been applied. Verify before acting on it.
```

Requests run at `temperature: 0` — advice that changes between runs is advice
nobody can act on. The transport is the standard library, so the advisory layer
adds no dependency either. Set `--model` to use something other than
`gemini-2.0-flash`.

## Configuration

`.dockerfile-optimizer.toml`, or `[tool.dockerfile-optimizer]` in `pyproject.toml`,
discovered by walking up from the working directory:

```toml
disabled_rules = ["MISSING_HEALTHCHECK"]
fail_on = "high"
format = "text"
```

An invalid value is an error, not a silent fallback — a typo in `fail_on` must not
leave a team believing they have a gate they do not have.

## CI

SARIF output uploads to GitHub code scanning, putting findings inline on the diff:

```yaml
- run: stow analyze Dockerfile --format sarif > dockerfile.sarif
  continue-on-error: true
- uses: github/codeql-action/upload-sarif@v3
  with:
    sarif_file: dockerfile.sarif
```

As a pre-commit hook:

```yaml
- repo: https://github.com/Oruwe/dockerfile-optimizer
  rev: main
  hooks:
    - id: dockerfile-optimizer
```

## Try it

```bash
./stow analyze examples/Dockerfile.bad --format text    # 13 findings
./stow analyze examples/Dockerfile.good                 # clean
./stow refactor examples/Dockerfile.bad                 # hardened rewrite
```

## Development

```bash
pip install -e ".[dev]"
ruff check . && mypy app && pytest
```

On Windows, prefix with the interpreter: `python -m pytest`, `python -m ruff check .`.

The agent gates itself: CI runs `stow analyze Dockerfile` against this repo's own
image definition, and `stow refactor examples/Dockerfile.good --check` proves the
refactor engine is a no-op on already-optimal input.

## Scope

No network access, so the agent cannot confirm a digest exists or report CVEs in a
base image. No container execution, so runtime failures and true layer sizes are
outside what static analysis can see. Patterns outside the table above are
reported, never guessed at.
