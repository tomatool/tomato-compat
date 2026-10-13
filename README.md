# tomato-compatibility

Regression tests for [tomato](https://github.com/tomatool/tomato), as a
GitHub Action. They live here rather than in tomato's own repository, so
tomato keeps its unit and integration tests and this repository keeps what
checks a release against the last one and against what a user does.

The action runs a released baseline and a candidate build on the same inputs
and diffs the results, then drives the candidate the way a person does: the
`tomato init` wizard on a pseudo-terminal, a project without containers, the
web UI over its HTTP API, and the baseline release's own integration suite.
Every removal, change or failure is a *break*. A break is allowed when the
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
| First run | `tomato init` driven on a pseudo-terminal with every dependency selected and no app, then `validate` and `run` | the wizard writes nothing, the project does not validate, or any of its scenarios fails |
| Reports and events | a shell-only project run with Docker unreachable, with JUnit and Cucumber outputs, then with `--format tomato` | Docker required for a container-free project, a wrong exit code, a malformed report, or step events without `stepIndex` and `durationMs` |
| Web UI | `tomato ui` started on that project and driven over HTTP | the page or `/api/config` not served, a run or a filtered run not started, a second concurrent run not refused with 409 |
| Baseline's own suite | the baseline tag's `tests/` run with the candidate binary | a scenario that fails |
| Version label (on tags) | the candidate's version against the baseline tag | a patch bump that carries additions or breaks |

What it cannot do: judge severity, or tell a flaky container from a bug. A
person does that from the summary.

## Use

In the tomato repository, after building the binary:

```yaml
- uses: tomatool/tomato-compatibility@main
  with:
    candidate: ./bin/tomato              # built from this PR
    allow: compatibility/breaking.txt    # documented breaks live next to the changelog
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
| `allow` | `compatibility/breaking.txt` | Allowlist of documented breaks |
| `first-run` | `true` | Drive the wizard and run what it writes; needs Docker |
| `reports` | `true` | The container-free project, its reports and events |
| `ui` | `true` | The web UI over its API |
| `previous-suite` | `true` | Replay the baseline's own suite; needs Docker and Go |
| `candidate-version` | *(none)* | Version the candidate will ship as |

Outputs: `baseline`, the tag compared against, and `breaks`, the undocumented
breaks one per line. The job summary carries the full table. The whole run
takes about ten minutes, most of it the replay of the baseline's suite.

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
`firstrun`, `reports`, `ui`, `version`.

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
python3 regression.py --baseline-bin ./tomato-2.3.1 --candidate-bin ./tomato \
  --baseline-tag v2.3.1 --candidate-src ../tomato --corpus ./corpus
```

`--skip first-run,previous-suite` leaves out the two checks that need
Docker. `compatibility.py` on its own runs only the diff against the
baseline. Only the Python standard library is needed.
