#!/usr/bin/env python3
"""Backward-compatibility checks between two tomato binaries.

Runs a baseline release and a candidate build on the same inputs and
reports what the candidate removed or changed on tomato's stable surface:
step vocabulary, resource type names, CLI commands and flags, the GitHub
Action's inputs, and a corpus of configs. Optionally runs the baseline
release's own integration suite against the candidate.

Every removal or change is a "break". A break is allowed when the
allowlist names it, which is how an intentional, documented break is
recorded. The check fails on breaks that are not allowed.

Only the standard library is used, so it runs on any GitHub runner.
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass, field

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def fetch(url, timeout=120):
    """GET a URL as bytes. urllib first; curl when the local Python has no CA bundle."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except Exception as first:  # noqa: BLE001
        try:
            return subprocess.run(["curl", "-fsSL", url], stdout=subprocess.PIPE, check=True, timeout=timeout).stdout
        except Exception:  # noqa: BLE001
            raise first


def run(cmd, cwd=None, env=None, timeout=600):
    """Run a command, return (exit code, combined output without colours)."""
    p = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, timeout=timeout)
    return p.returncode, ANSI.sub("", p.stdout)


@dataclass
class Break:
    kind: str      # step, type, command, flag, input, case, scenario, version
    value: str
    detail: str = ""
    allowed: bool = False

    @property
    def key(self):
        return f"{self.kind} {self.value}"


@dataclass
class Report:
    rows: list = field(default_factory=list)      # (check, summary)
    breaks: list = field(default_factory=list)
    infos: list = field(default_factory=list)     # compatible changes worth seeing

    def add(self, check, summary):
        self.rows.append((check, summary))


# --- the stable surface ------------------------------------------------------

def steps_of(binary):
    code, out = run([binary, "steps", "--json"])
    if code != 0:
        raise SystemExit(f"{binary} steps --json failed:\n{out}")
    cats = json.loads(out)
    result = {}
    for cat in cats:
        for step in cat["steps"]:
            result[step["pattern"]] = (cat["name"], step.get("example", ""))
    return result


def pattern_matches(pattern, text):
    try:
        return re.search("(?i)" + pattern.replace("{resource}", '[^"]+'), text) is not None
    except re.error:
        return False


def check_steps(report, base_bin, cand_bin):
    base, cand = steps_of(base_bin), steps_of(cand_bin)
    removed, widened = [], []
    for pattern, (cat, example) in base.items():
        if pattern in cand:
            continue
        # A pattern that changed but still accepts its own example is a
        # widening, not a removal: the wording users wrote still works.
        first = example.splitlines()[0] if example else ""
        if first and any(c == cat and pattern_matches(p, first) for p, (c, _) in cand.items()):
            widened.append((cat, pattern))
        else:
            removed.append((cat, pattern))
    added = [p for p in cand if p not in base]
    for cat, pattern in removed:
        report.breaks.append(Break("step", pattern, f"{cat}: no candidate pattern accepts its example"))
    for cat, pattern in widened:
        report.infos.append(f"step pattern widened, old wording still matches: `{pattern}`")
    report.add("Step vocabulary", f"{len(base)} baseline, {len(cand)} candidate: "
               f"{len(removed)} removed, {len(widened)} widened, {len(added)} added")
    return len(added)


def types_of(binary):
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "tomato.yml"), "w") as f:
            f.write("version: 2\nresources:\n  probe:\n    type: compatibility-probe-not-a-type\n")
        _, out = run([binary, "validate", "--plain"], cwd=d)
    m = re.search(r"Valid types:\s*(.+)", out)
    if not m:
        raise SystemExit(f"could not read the type list from {binary}:\n{out}")
    return sorted(t.strip() for t in m.group(1).split(","))


def check_types(report, base_bin, cand_bin):
    base, cand = types_of(base_bin), types_of(cand_bin)
    removed = [t for t in base if t not in cand]
    added = [t for t in cand if t not in base]
    for t in removed:
        report.breaks.append(Break("type", t, "resource type no longer accepted"))
    report.add("Resource types", f"{len(base)} baseline, {len(cand)} candidate: "
               f"{len(removed)} removed, {len(added)} added" + (f" ({', '.join(added)})" if added else ""))
    return len(added)


def commands_of(binary):
    _, out = run([binary, "--help"])
    section = out.split("COMMANDS:", 1)[1].split("GLOBAL OPTIONS:", 1)[0] if "COMMANDS:" in out else ""
    cmds = []
    for line in section.splitlines():
        m = re.match(r"\s+([a-z][a-z-]*)(?:,\s*\S+)?\s{2,}", line)
        if m and m.group(1) != "help":
            cmds.append(m.group(1))
    return cmds


def flags_of(binary, cmd):
    _, out = run([binary, cmd, "--help"])
    section = out.split("OPTIONS:", 1)[1] if "OPTIONS:" in out else ""
    return sorted(set(re.findall(r"^\s+(--[a-z][a-z0-9-]*)", section, re.M)) - {"--help"})


def check_cli(report, base_bin, cand_bin):
    base_cmds, cand_cmds = commands_of(base_bin), commands_of(cand_bin)
    removed_cmds = [c for c in base_cmds if c not in cand_cmds]
    removed_flags = 0
    for c in removed_cmds:
        report.breaks.append(Break("command", c, "command gone from --help"))
    for c in base_cmds:
        if c in removed_cmds:
            continue
        gone = [f for f in flags_of(base_bin, c) if f not in flags_of(cand_bin, c)]
        for f in gone:
            removed_flags += 1
            report.breaks.append(Break("flag", f"{c} {f}", "flag gone from the command's --help"))
    added_cmds = [c for c in cand_cmds if c not in base_cmds]
    report.add("CLI commands and flags", f"{len(base_cmds)} commands checked: {len(removed_cmds)} removed, "
               f"{removed_flags} flags removed, {len(added_cmds)} commands added")


def inputs_of(text):
    inputs, inside = [], False
    for line in text.splitlines():
        if re.match(r"^inputs:\s*$", line):
            inside = True
            continue
        if inside and re.match(r"^\S", line):
            break
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if inside and m:
            inputs.append(m.group(1))
    return inputs


def check_action_inputs(report, repo, tag, candidate_src):
    path = os.path.join(candidate_src, "action.yml")
    if not os.path.exists(path):
        report.add("GitHub Action inputs", "skipped: no action.yml in the candidate checkout")
        return
    url = f"https://raw.githubusercontent.com/{repo}/{tag}/action.yml"
    try:
        base_text = fetch(url).decode()
    except Exception as e:  # noqa: BLE001
        report.add("GitHub Action inputs", f"skipped: could not fetch {url} ({e})")
        return
    with open(path) as f:
        cand_text = f.read()
    base, cand = inputs_of(base_text), inputs_of(cand_text)
    removed = [i for i in base if i not in cand]
    for i in removed:
        report.breaks.append(Break("input", i, "action input gone"))
    report.add("GitHub Action inputs", f"{len(base)} baseline, {len(cand)} candidate: {len(removed)} removed")


# --- the corpus ---------------------------------------------------------------

def validate_in(binary, case_dir):
    code, out = run([binary, "validate", "--plain"], cwd=case_dir)
    return ("ok" if code == 0 else "fail"), out


def check_corpus(report, base_bin, cand_bin, corpus):
    if not corpus or not os.path.isdir(corpus):
        report.add("Config corpus", "skipped: no corpus directory")
        return
    cases = sorted(d for d in os.listdir(corpus) if os.path.exists(os.path.join(corpus, d, "tomato.yml")))
    changed = expect_failed = 0
    for name in cases:
        case_dir = os.path.join(corpus, name)
        cand_outcome, cand_out = validate_in(cand_bin, case_dir)
        expect_path = os.path.join(case_dir, "expect")
        if os.path.exists(expect_path):
            with open(expect_path) as f:
                lines = [l.rstrip("\n") for l in f if l.strip()]
            want, phrases = lines[0].strip(), lines[1:]
            missing = [p for p in phrases if p not in cand_out]
            if cand_outcome != want or missing:
                expect_failed += 1
                why = f"expected {want}, got {cand_outcome}" if cand_outcome != want else "message lacks: " + "; ".join(missing)
                report.breaks.append(Break("case", name, why + "\n" + cand_out.strip()[-600:]))
            continue
        base_outcome, _ = validate_in(base_bin, case_dir)
        if base_outcome == "ok" and cand_outcome == "fail":
            changed += 1
            report.breaks.append(Break("case", name, "loaded on the baseline, refused by the candidate:\n" + cand_out.strip()[-600:]))
        elif base_outcome == "fail" and cand_outcome == "ok":
            report.infos.append(f"corpus case `{name}` was refused by the baseline and loads on the candidate")
    report.add("Config corpus", f"{len(cases)} cases: {expect_failed} failed their expectation, "
               f"{changed} loaded on the baseline but not on the candidate")


# --- the baseline's own suite -------------------------------------------------

def check_previous_suite(report, repo, tag, cand_bin, workdir):
    url = f"https://github.com/{repo}/archive/refs/tags/{tag}.tar.gz"
    data = fetch(url, timeout=300)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        tf.extractall(workdir)
    root = os.path.join(workdir, os.listdir(workdir)[0])
    env = dict(os.environ, TOMATO_BIN=cand_bin)
    code, out = run([cand_bin, "run", "-c", "tests/tomato.yml", "--format", "tomato", "--quiet"],
                    cwd=root, env=env, timeout=3600)
    failed, total = [], 0
    for line in out.splitlines():
        if not line.startswith("TOMATO_EVENT:"):
            continue
        ev = json.loads(line[len("TOMATO_EVENT:"):])
        if ev.get("type") == "scenario_end":
            total += 1
            if ev.get("status") == "failed":
                failed.append(f"{os.path.basename(ev.get('file', ''))}/{ev.get('scenario')}")
    if total == 0:
        report.breaks.append(Break("scenario", "(suite did not run)", out.strip()[-1200:]))
    for name in failed:
        report.breaks.append(Break("scenario", name, f"from {tag}'s own suite, fails on the candidate"))
    report.add(f"{tag}'s suite on the candidate", f"{total} scenarios, {len(failed)} failed, exit {code}")


# --- version labelling --------------------------------------------------------

def semver(s):
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", s or "")
    return tuple(int(x) for x in m.groups()) if m else None


def check_version(report, base_tag, cand_version, additions):
    b, c = semver(base_tag), semver(cand_version)
    if not b or not c:
        report.add("Version label", "skipped: no candidate version given")
        return
    patch_bump = c[:2] == b[:2] and c[2] > b[2]
    hard_breaks = [x for x in report.breaks if x.kind != "version"]
    if patch_bump and (additions or hard_breaks):
        report.breaks.append(Break("version", cand_version,
                                   f"a patch over {base_tag} that carries {additions} additions and {len(hard_breaks)} breaks; "
                                   "the stability policy makes that a minor at least"))
    report.add("Version label", f"{base_tag} to {cand_version}: {'patch' if patch_bump else 'not a patch'} bump, "
               f"{additions} additions, {len(hard_breaks)} breaks")


# --- allowlist and output -----------------------------------------------------

def load_allow(path):
    allowed = set()
    if path and os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    allowed.add(re.sub(r"\s+", " ", line, count=1))
    return allowed


def write_summary(report, base_tag, path):
    lines = ["## tomato regression", "", f"Baseline: `{base_tag}`", "", "| Check | Result |", "|---|---|"]
    lines += [f"| {c} | {s} |" for c, s in report.rows]
    not_allowed = [b for b in report.breaks if not b.allowed]
    allowed = [b for b in report.breaks if b.allowed]
    if not_allowed:
        lines += ["", f"### ❌ {len(not_allowed)} undocumented break(s)", "",
                  "Each needs an entry in the allowlist, next to its changelog entry:", "", "```"]
        lines += [b.key for b in not_allowed] + ["```", ""]
        for b in not_allowed:
            lines += [f"- `{b.key}`: {b.detail.splitlines()[0] if b.detail else ''}"]
            extra = b.detail.splitlines()[1:]
            if extra:
                lines += ["", "  ```", *("  " + l for l in extra), "  ```"]
    if allowed:
        lines += ["", f"### ✅ {len(allowed)} documented break(s)", ""]
        lines += [f"- `{b.key}`" for b in allowed]
    if report.infos:
        lines += ["", "### Compatible changes", ""] + [f"- {i}" for i in report.infos]
    text = "\n".join(lines) + "\n"
    if path:
        with open(path, "a") as f:
            f.write(text)
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline-bin", required=True)
    ap.add_argument("--candidate-bin", required=True)
    ap.add_argument("--baseline-tag", required=True, help="release tag the baseline binary came from")
    ap.add_argument("--repo", default="tomatool/tomato")
    ap.add_argument("--candidate-src", default=".", help="checkout of the candidate, for action.yml")
    ap.add_argument("--corpus", default="")
    ap.add_argument("--allow", default="")
    ap.add_argument("--previous-suite", action="store_true")
    ap.add_argument("--candidate-version", default="")
    ap.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    ap.add_argument("--breaks-out", default="", help="write the undocumented break keys here, one per line")
    args = ap.parse_args()

    report = Report()
    additions = check_steps(report, args.baseline_bin, args.candidate_bin)
    additions += check_types(report, args.baseline_bin, args.candidate_bin)
    check_cli(report, args.baseline_bin, args.candidate_bin)
    check_action_inputs(report, args.repo, args.baseline_tag, args.candidate_src)
    check_corpus(report, args.baseline_bin, args.candidate_bin, args.corpus)
    if args.previous_suite:
        with tempfile.TemporaryDirectory() as d:
            check_previous_suite(report, args.repo, args.baseline_tag, os.path.abspath(args.candidate_bin), d)
    check_version(report, args.baseline_tag, args.candidate_version, additions)

    allowed = load_allow(args.allow)
    for b in report.breaks:
        b.allowed = b.key in allowed
    text = write_summary(report, args.baseline_tag, args.summary)
    print(text)
    not_allowed = [b for b in report.breaks if not b.allowed]
    if args.breaks_out:
        with open(args.breaks_out, "w") as f:
            f.write("".join(b.key + "\n" for b in not_allowed))
    sys.exit(1 if not_allowed else 0)


if __name__ == "__main__":
    main()
