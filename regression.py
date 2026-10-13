#!/usr/bin/env python3
"""Regression tests for tomato: everything in one run.

Backward compatibility with a released baseline (compatibility.py), then
the behaviour a user meets (behaviour.py): the first run through the
wizard, a container-free project with its reports and event stream, the
web UI over its API, and the baseline release's own suite replayed
against the candidate. One summary, one allowlist, one exit code.
"""

import argparse
import os
import sys
import tempfile

import behaviour
import compatibility as c


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline-bin", required=True)
    ap.add_argument("--candidate-bin", required=True)
    ap.add_argument("--baseline-tag", required=True)
    ap.add_argument("--repo", default="tomatool/tomato")
    ap.add_argument("--candidate-src", default=".")
    ap.add_argument("--corpus", default="")
    ap.add_argument("--allow", default="")
    ap.add_argument("--candidate-version", default="")
    ap.add_argument("--skip", default="", help="comma-separated: first-run, reports, ui, previous-suite")
    ap.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    ap.add_argument("--breaks-out", default="")
    args = ap.parse_args()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    cand = os.path.abspath(args.candidate_bin)
    env = dict(os.environ)

    report = c.Report()
    additions = c.check_steps(report, args.baseline_bin, cand)
    additions += c.check_types(report, args.baseline_bin, cand)
    c.check_cli(report, args.baseline_bin, cand)
    c.check_action_inputs(report, args.repo, args.baseline_tag, args.candidate_src)
    c.check_corpus(report, args.baseline_bin, cand, args.corpus)
    if "reports" not in skip:
        behaviour.check_reports(report, cand, env)
    if "ui" not in skip:
        behaviour.check_ui(report, cand, env)
    if "first-run" not in skip:
        behaviour.check_first_run(report, cand, env)
    if "previous-suite" not in skip:
        with tempfile.TemporaryDirectory() as d:
            c.check_previous_suite(report, args.repo, args.baseline_tag, cand, d)
    c.check_version(report, args.baseline_tag, args.candidate_version, additions)

    allowed = c.load_allow(args.allow)
    for b in report.breaks:
        b.allowed = b.key in allowed
    print(c.write_summary(report, args.baseline_tag, args.summary))
    not_allowed = [b for b in report.breaks if not b.allowed]
    if args.breaks_out:
        with open(args.breaks_out, "w") as f:
            f.write("".join(b.key + "\n" for b in not_allowed))
    sys.exit(1 if not_allowed else 0)


if __name__ == "__main__":
    main()
