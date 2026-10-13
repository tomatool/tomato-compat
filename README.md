# tomato-compat

A GitHub Action that checks a build of [tomato](https://github.com/tomatool/tomato)
against a released baseline and fails on anything the candidate removed or
changed on tomato's stable surface without saying so.

It runs the baseline release and the candidate on the same inputs and diffs
the results. Every removal or change is a *break*. A break is allowed when the
allowlist names it, which is how an intentional, documented break is recorded
next to its changelog entry. The check fails on breaks that are not allowed.

## What it checks

| Check | How | A break is |
|---|---|---|
| Step vocabulary | `tomato steps --json` on both binaries | a baseline pattern that no candidate pattern in the same resource accepts, tried with the baseline's own example (a widened pattern is compatible) |
| Resource types | the `Valid types:` list `tomato validate` prints for an unknown type | a type name or alias gone |
| CLI | `--help` of every command on both binaries | a command or a flag gone |
| GitHub Action inputs | `action.yml` at the baseline tag against the candidate checkout | an input gone |
| Config corpus | `tomato validate --plain` in every case directory | a case with an `expect` file that does not meet it; a case without one that the baseline loaded and the candidate refuses |
| Baseline's own suite (opt-in) | the baseline tag's `tests/` run with the candidate binary | a scenario that fails |
| Version label (opt-in) | the candidate's version against the baseline tag | a patch bump that carries additions or breaks |

What it cannot do: judge severity, tell a flaky container from a bug, or
drive `tomato init` and `tomato update`, which need a terminal. tomato's own
CI runs the generated project end to end.

## Use

In the tomato repository, after building the binary:

```yaml
- uses: tomatool/tomato-compat@main
  with:
    candidate: ./bin/tomato        # built from this PR
    allow: compat/breaking.txt     # documented breaks live next to the changelog
```

On a tag, also pass the version so a patch cannot carry minor-version material:

```yaml
    candidate-version: ${{ github.ref_name }}
```

Inputs:

| Input | Default | Meaning |
|---|---|---|
| `candidate` | *(build from the checkout)* | Path to the binary under test |
| `candidate-src` | `.` | Checkout the candidate's `action.yml` is read from |
| `baseline` | `latest` | Release tag to compare against |
| `repo` | `tomatool/tomato` | Where the baseline release is downloaded from |
| `corpus` | *(bundled)* | Directory of config cases |
| `allow` | `compat/breaking.txt` | Allowlist of documented breaks |
| `previous-suite` | `false` | Run the baseline's own integration suite against the candidate; needs Docker and Go |
| `candidate-version` | *(none)* | Version the candidate will ship as |

Outputs: `baseline`, the tag compared against, and `breaks`, the undocumented
breaks one per line. The job summary carries the full table.

## The allowlist

One break per line, `kind value`, exactly as the summary prints it. Comments
start with `#`.

```
# v2.3.0: every resource is reset before every scenario, see CHANGELOG Removed
flag run --no-reset
input no-reset
case resource-reset-false
```

Kinds: `step` (the pattern), `type`, `command`, `flag` (`<command> --flag`),
`input`, `case` (corpus directory name), `scenario` (`<file>/<name>`),
`version`.

## The corpus

A directory per case holding a `tomato.yml`. An optional `expect` file holds
`ok` or `fail` on its first line and, after it, phrases the candidate's output
must contain:

```
fail
looks like a tomato v1 config
```

A case without `expect` is compared across the two binaries: loading on the
baseline and failing on the candidate is a break; the reverse is an addition.
The bundled corpus covers the config version, the removed types and
settings, the unmanaged-resource refusal, every alias, the Kafka preset,
templated app env, and the reset opt-outs v2.3.0 removed.

## Run it locally

```sh
python3 compat.py --baseline-bin ./tomato-2.3.1 --candidate-bin ./tomato \
  --baseline-tag v2.3.1 --candidate-src ../tomato --corpus ./corpus
```

Only the Python standard library is needed.
