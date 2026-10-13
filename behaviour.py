"""Behaviour checks: what a user does with tomato, run for real.

The first-run path through the `tomato init` wizard, a container-free
project with its reports and event stream, and the web UI through its
HTTP API. The wizard and the UI are driven the way a person drives them,
on a pseudo-terminal and over HTTP, so a change in either shows up here
rather than in a new user's first hour.
"""

import json
import os
import pty
import re
import select
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET

from compatibility import ANSI, Break, run


# --- a pseudo-terminal for the wizard ---------------------------------------

def drive_tui(cmd, cwd, script, timeout=90):
    """Run a terminal program on a pseudo-terminal and feed it keys.

    `script` is a list of (wait_for, keys, settle): wait until any of the
    `wait_for` substrings shows in the output (or the timeout), pause
    `settle` seconds, then send `keys`. Returns (exit code, output).
    """
    pid, fd = pty.fork()
    if pid == 0:  # the child: become the program
        os.chdir(cwd)
        os.environ.update(TERM="xterm-256color", COLUMNS="100", LINES="40")
        os.execvp(cmd[0], cmd)
    out = ""

    def pump(seconds):
        nonlocal out
        end = time.time() + seconds
        while True:
            left = end - time.time()
            if left <= 0:
                return
            r, _, _ = select.select([fd], [], [], min(left, 0.2))
            if r:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    return
                if not data:
                    return
                out += data.decode(errors="replace")

    for wait_for, keys, settle in script:
        deadline = time.time() + timeout
        while wait_for and not any(w in ANSI.sub("", out) for w in wait_for) and time.time() < deadline:
            pump(0.2)
        pump(settle)
        os.write(fd, keys.encode())
    # drain until the program exits, so it never blocks on a full pty
    deadline = time.time() + timeout
    while time.time() < deadline:
        wpid, status = os.waitpid(pid, os.WNOHANG)
        if wpid:
            pump(0.2)
            os.close(fd)
            code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status)
            return code, ANSI.sub("", out)
        pump(0.2)
    os.kill(pid, 9)
    os.waitpid(pid, 0)
    os.close(fd)
    return 124, ANSI.sub("", out)


INIT_SCRIPT = [
    (["PostgreSQL", "Select", "dependenc"], "a", 0.4),      # select every dependency
    ([], "\r", 0.5),                                        # next: the app runner
    (["Skip", "Docker", "runner"], "j", 0.2), ([], "j", 0.2), ([], "\r", 0.5),  # skip the app
    (["Create", "Confirm", "confirm", "Generate"], "\r", 0.3),  # confirm
]


def check_first_run(report, cand_bin, env):
    """`tomato init` with everything selected and no app, then validate and run."""
    d = tempfile.mkdtemp(prefix="tomato-first-run-")
    try:
        code, out = drive_tui([cand_bin, "init"], d, INIT_SCRIPT)
        files = sorted(os.path.relpath(os.path.join(r, f), d) for r, _, fs in os.walk(d) for f in fs)
        if code != 0 or "tomato.yml" not in files:
            report.breaks.append(Break("firstrun", "init", f"the wizard exited {code} and wrote {files or 'nothing'}\n{out[-800:]}"))
            report.add("First run: init, validate, run", "the wizard did not write a project")
            return
        vcode, vout = run([cand_bin, "validate", "--plain"], cwd=d, env=env)
        if vcode != 0:
            report.breaks.append(Break("firstrun", "validate", "the generated project does not validate:\n" + vout.strip()[-800:]))
            report.add("First run: init, validate, run", "generated project fails validate")
            return
        feature = open(os.path.join(d, "features", "example.feature")).read() if "features/example.feature" in files else ""
        want = len(re.findall(r"^\s*Scenario( Outline)?:", feature, re.M))
        rcode, rout = run([cand_bin, "run"], cwd=d, env=env, timeout=900)
        m = re.search(r"(?m)^(\d+) scenarios \((\d+) passed\)$", rout)
        if rcode != 0 or not m or m.group(1) != m.group(2) or int(m.group(1)) != want:
            tail = "\n".join(l for l in rout.splitlines() if l.strip())[-1200:]
            report.breaks.append(Break("firstrun", "run", f"expected {want} scenarios, all passed, exit 0; got exit {rcode}:\n{tail}"))
            report.add("First run: init, validate, run", f"generated project fails its own suite (exit {rcode})")
            return
        report.add("First run: init, validate, run", f"wizard, validate and run pass: {want} scenarios green")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- a container-free project: reports and the event stream ---------------------

SHELL_PROJECT = {
    "tomato.yml": "version: 2\nsettings:\n  timeout: 1m\n  output: \"pretty,junit:reports/tomato.xml,cucumber:reports/cucumber.json\"\n"
                  "resources:\n  shell:\n    type: shell\nfeatures:\n  paths:\n    - ./features\n",
    "features/shell.feature": "Feature: Shell only\n\n  Scenario: Passing\n    When \"shell\" runs \"echo hello\"\n"
                              "    Then \"shell\" succeeds\n    And \"shell\" stdout contains \"hello\"\n\n"
                              "  Scenario: Failing on purpose\n    When \"shell\" runs \"echo hello\"\n"
                              "    Then \"shell\" stdout contains \"nope\"\n",
}


def write_project(d, files):
    for name, body in files.items():
        path = os.path.join(d, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(body)


def check_reports(report, cand_bin, env):
    """A shell-only project runs without Docker, exits 1 on a failure, and writes both reports."""
    d = tempfile.mkdtemp(prefix="tomato-reports-")
    try:
        write_project(d, SHELL_PROJECT)
        no_docker = dict(env, DOCKER_HOST="unix:///nonexistent/docker.sock")
        code, out = run([cand_bin, "run"], cwd=d, env=no_docker)
        problems = []
        if code != 1:
            problems.append(f"exit {code}, expected 1 for one failing scenario (or Docker was required for a container-free project)")
        if "2 scenarios (1 passed, 1 failed)" not in out:
            problems.append("summary line missing or changed")
        try:
            root = ET.parse(os.path.join(d, "reports", "tomato.xml")).getroot()
            suites = root.findall(".//testsuite")
            if len(suites) != 1 or suites[0].get("tests") != "2" or suites[0].get("failures") != "1":
                problems.append("junit: expected one testsuite with tests=2 failures=1")
        except Exception as e:  # noqa: BLE001
            problems.append(f"junit report unreadable: {e}")
        try:
            cuc = json.load(open(os.path.join(d, "reports", "cucumber.json")))
            statuses = [s["result"]["status"] for f in cuc for e in f.get("elements", []) for s in e.get("steps", [])]
            if statuses.count("failed") != 1 or statuses.count("passed") != 4:
                problems.append(f"cucumber: step statuses {statuses}")
        except Exception as e:  # noqa: BLE001
            problems.append(f"cucumber report unreadable: {e}")
        _, ev = run([cand_bin, "run", "--format", "tomato"], cwd=d, env=no_docker)
        events = [json.loads(l[len("TOMATO_EVENT:"):]) for l in ev.splitlines() if l.startswith("TOMATO_EVENT:")]
        ends = [e for e in events if e.get("type") == "step_end"]
        if not ends or any("stepIndex" not in e or "durationMs" not in e for e in ends):
            problems.append("tomato event stream lacks step_end events with stepIndex and durationMs")
        for p in problems:
            report.breaks.append(Break("reports", p.split(":")[0].split(",")[0], p + "\n" + out.strip()[-600:]))
        report.add("Container-free run, reports, events",
                   "exit code, summary, junit, cucumber and event stream as expected" if not problems else f"{len(problems)} problem(s)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- the web UI through its API ------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method, url, timeout=5):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def wait_for(pred, seconds, every=0.5):
    end = time.time() + seconds
    while time.time() < end:
        if pred():
            return True
        time.sleep(every)
    return False


def check_ui(report, cand_bin, env):
    """`tomato ui` serves the page and the config, runs the suite and a filtered subset on request."""
    d = tempfile.mkdtemp(prefix="tomato-ui-")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = None
    problems = []
    try:
        write_project(d, SHELL_PROJECT)
        proc = subprocess.Popen([cand_bin, "ui", "--no-browser", "--port", str(port)], cwd=d, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if not wait_for(lambda: http("GET", base + "/")[0] == 200 if _reachable(port) else False, 20):
            problems.append("the UI did not answer GET / within 20 s")
        else:
            status, body = http("GET", base + "/api/config")
            try:
                cfg = json.loads(body)
                res = cfg.get("resources") or []
                names = [r.get("name") for r in res] if isinstance(res, list) else list(res.keys())
                if status != 200 or "shell" not in names:
                    problems.append(f"GET /api/config: status {status}, resources {names}")
            except json.JSONDecodeError:
                problems.append(f"GET /api/config is not JSON (status {status})")
            report_path = os.path.join(d, "reports", "tomato.xml")
            status, body = http("POST", base + "/api/run")
            if status != 200 or "started" not in body:
                problems.append(f"POST /api/run: status {status}, body {body[:120]!r}")
            status, _ = http("POST", base + "/api/run")
            if status != 409:
                problems.append(f"a second POST /api/run while running answered {status}, expected 409")
            if not wait_for(lambda: os.path.exists(report_path), 60):
                problems.append("the run started from the UI wrote no report within 60 s")
            else:
                os.remove(report_path)
                status, _ = http("POST", base + "/api/run?scenario=Passing")
                if status != 200:
                    problems.append(f"POST /api/run?scenario=Passing answered {status}")
                elif not wait_for(lambda: os.path.exists(report_path), 60):
                    problems.append("the filtered run wrote no report within 60 s")
                else:
                    suites = ET.parse(report_path).getroot().findall(".//testsuite")
                    if not suites or suites[0].get("tests") != "1":
                        problems.append("the filtered run did not run exactly one scenario")
            status, _ = http("POST", base + "/api/stop")
            if status != 409:
                problems.append(f"POST /api/stop with nothing running answered {status}, expected 409")
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(d, ignore_errors=True)
    for p in problems:
        report.breaks.append(Break("ui", p.split(":")[0], p))
    report.add("Web UI over its API", "page, config, run, filtered run, conflict and stop as expected" if not problems else f"{len(problems)} problem(s)")


def _reachable(port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0
