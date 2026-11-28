"""Runs Whooper's tests, lib/test_whooper.py, in parallel, as lib/run-tests does:

  lib/run-tests [-k <pattern>]... [-j <workers>]

Every test runs twice at once: under the usual PATH, and with /usr/bin:/bin first, where /bin/bash 3.2
and Apple's git run the scripts, since bash 3.2 has caught bugs that bash 5 lets through. Under each
PATH, worker processes take the next test as each finishes one, the slowest first by how long each
took when it last ran, so a long test does not start last. A worker runs each test the way unittest
runs a whole module, with setUpModule, setUpClass, and their cleanups around it, so no test sees what
another left behind. The workers share one Go build cache, which setUpModule takes from
WHOOPER_TEST_GOCACHE, and lib/python runs the Python in WHOOPER_TEST_PYTHON, the one running this,
without running uv twice first, since starting a program is slow on a Mac whose security software
checks each one, and lib/python is most of the programs the tests start. At most HEAVY_AT_ONCE tests
marked memory_heavy run at once.

It prints a character per test as unittest does, then each failure and error with its traceback, what
a test wrote outside its own checks, each warning a test raised, and the counts. It exits 1 when a test
fails or errs or a ResourceWarning appears, and 0 otherwise.
"""
import argparse
from collections import Counter
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from time import monotonic
import unittest
import warnings

LIB = Path(__file__).resolve().parent
ROOT = LIB.parent
PATHS = {"usual PATH": os.environ["PATH"], "/usr/bin:/bin first": f"/usr/bin:/bin:{os.environ['PATH']}"}
# How long each test took when it last ran, by test ID, which only sets the order of the next run.
DURATIONS = Path(tempfile.gettempdir()) / "whooper-test-durations.json"
# How many tests marked memory_heavy, which run gopls, TypeScript, or Vitest, may run at once under both PATHs together.
HEAVY_AT_ONCE = 2
LINE = "-" * 70


class Result(unittest.TestResult):
    """A unittest result that also keeps how long each test took, by test ID."""

    def __init__(self):
        super().__init__()
        self.durations = {}

    def addDuration(self, test, elapsed):
        self.durations[test.id()] = elapsed


def work():
    """Runs as a worker: reads a JSON list of test IDs per line from standard input, runs those tests, and answers each
    line with a JSON line of their results on standard output."""
    # python -I leaves the script's folder off sys.path; unittest discover adds the folder it searches the same way.
    sys.path.insert(0, str(LIB))
    # The requests and replies move to file descriptors of their own: the tests, and the processes they start, read
    # nothing and write to a file of each request's own, so none can take a request or break a reply.
    requests = os.fdopen(os.dup(0), encoding="utf-8")
    replies = os.fdopen(os.dup(1), "w", encoding="utf-8")
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    stderr = os.dup(2)
    caught = []
    show = warnings.showwarning
    warnings.showwarning = lambda message, category, filename, lineno, file=None, line=None: caught.append(
        warnings.formatwarning(message, category, filename, lineno, line))
    loader = unittest.TestLoader()
    with requests, replies:
        for request in requests:
            result = Result()
            with tempfile.TemporaryFile() as output:
                os.dup2(output.fileno(), 1)
                os.dup2(output.fileno(), 2)
                try:
                    loader.loadTestsFromNames(json.loads(request)).run(result)
                    # Finalizes what the tests left unclosed, so its ResourceWarning comes with them.
                    gc.collect()
                finally:
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os.dup2(stderr, 1)
                    os.dup2(stderr, 2)
                output.seek(0)
                text = output.read().decode(errors="replace")
            replies.write(json.dumps({
                "ran": result.testsRun,
                "failures": [(str(test), trace) for test, trace in result.failures],
                "errors": [(str(test), trace) for test, trace in result.errors],
                "unexpected_successes": [str(test) for test in result.unexpectedSuccesses],
                "skipped": [(str(test), reason) for test, reason in result.skipped],
                "durations": result.durations,
                "warnings": caught,
                "output": text,
            }) + "\n")
            replies.flush()
            caught.clear()
    # A warning as the worker exits goes to its error output, which the summary prints.
    warnings.showwarning = show


class Schedule:
    """The tests each PATH has yet to run, slowest first."""

    def __init__(self, tests, heavy):
        self.waiting = {name: list(tests) for name in PATHS}
        self.heavy, self.heavy_running = heavy, 0
        self.changed = threading.Condition()

    def take(self, name):
        """The next test to run under PATH name, or None once none is left: the first waiting, but not one marked
        memory_heavy while HEAVY_AT_ONCE of those run."""
        with self.changed:
            while self.waiting[name]:
                for i, test in enumerate(self.waiting[name]):
                    if test not in self.heavy:
                        return self.waiting[name].pop(i)
                    if self.heavy_running < HEAVY_AT_ONCE:
                        self.heavy_running += 1
                        return self.waiting[name].pop(i)
                self.changed.wait()
            return None

    def done(self, test):
        if test in self.heavy:
            with self.changed:
                self.heavy_running -= 1
                self.changed.notify_all()


class Results:
    """What each test's runs gave, by PATH, and the progress line unittest would print."""

    def __init__(self):
        self.lock = threading.Lock()
        self.replies = {name: {} for name in PATHS}

    def add(self, name, test, reply):
        with self.lock:
            self.replies[name][test] = reply
            if reply["errors"]:
                mark = "E"
            elif reply["failures"]:
                mark = "F"
            elif reply["unexpected_successes"]:
                mark = "u"
            elif reply["skipped"]:
                mark = "s"
            else:
                mark = "."
            sys.stderr.write(mark)
            sys.stderr.flush()


def drive(name, env, schedule, results, logs):
    """Runs the tests of schedule under PATH name in a worker process, in env, until none is left, and starts another
    worker when one exits in the middle of a test."""
    worker = log = None
    while (test := schedule.take(name)) is not None:
        if worker is None:
            log = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
            logs.append((name, log))
            # Started from the repo's root, as lib/python -m unittest discover -s lib runs there.
            worker = subprocess.Popen([sys.executable, "-I", "-W", "always::ResourceWarning", "lib/run_tests.py",
                                       "--worker"], cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=log, text=True, encoding="utf-8")
        try:
            worker.stdin.write(json.dumps([test]) + "\n")
            worker.stdin.flush()
            reply = worker.stdout.readline()
        except BrokenPipeError:
            reply = ""
        if reply:
            reply = json.loads(reply)
        else:
            code = worker.wait()
            worker.stdin.close()
            worker.stdout.close()
            worker = None
            reply = {"ran": 1, "failures": [], "unexpected_successes": [], "skipped": [], "durations": {},
                     "warnings": [], "output": "",
                     "errors": [(test, f"The worker running it exited with code {code}; WORKER ERROR OUTPUT below"
                                       " holds what it wrote, if anything.")]}
        schedule.done(test)
        results.add(name, test, reply)
    if worker is not None:
        worker.stdin.close()
        worker.wait()
        worker.stdout.close()


def tests_in(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from tests_in(test)
        else:
            yield test


def main():
    parser = argparse.ArgumentParser(prog="lib/run-tests", description=__doc__.split("\n\n")[1].replace("\n", " "))
    parser.add_argument("-k", dest="patterns", action="append", default=[], metavar="PATTERN",
                        help="run only the tests whose names match PATTERN, as unittest's -k; may repeat")
    parser.add_argument("-j", dest="workers", type=int, default=max(1, os.cpu_count() // 2), metavar="WORKERS",
                        help="worker processes per PATH (default: half the CPUs, %(default)s)")
    args = parser.parse_args()

    loader = unittest.TestLoader()
    loader.testNamePatterns = [pattern if "*" in pattern else f"*{pattern}*" for pattern in args.patterns] or None
    tests = list(tests_in(loader.discover(str(LIB), top_level_dir=str(LIB))))
    if loader.errors:
        sys.exit("".join(loader.errors))
    try:
        last = json.loads(DURATIONS.read_text())
    except (OSError, ValueError):
        last = {}
    # A test that has not run yet goes first: it is likely the one being written.
    order = sorted((test.id() for test in tests), key=lambda test: -last.get(test, float("inf")))
    heavy = {test.id() for test in tests if getattr(getattr(test, test._testMethodName), "memory_heavy", False)}
    schedule, results, logs = Schedule(order, heavy), Results(), []
    started = monotonic()
    with tempfile.TemporaryDirectory(prefix="whooper-tests-", ignore_cleanup_errors=True) as shared:
        threads = [threading.Thread(target=drive, daemon=True, args=(
                       name, {**os.environ, "PATH": path, "WHOOPER_TEST_GOCACHE": str(Path(shared) / "go-build"),
                              "WHOOPER_TEST_PYTHON": sys.executable},
                       schedule, results, logs))
                   for name, path in PATHS.items() for _ in range(min(args.workers, len(order)))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    elapsed = monotonic() - started
    sys.stderr.write("\n")

    failed, resource_warnings = False, 0
    for name, replies in results.replies.items():
        for test in order:
            reply = replies[test]
            for flavor, key in [("ERROR", "errors"), ("FAIL", "failures")]:
                for description, trace in reply[key]:
                    failed = True
                    print(f"{'=' * 70}\n{flavor}: {description} [{name}]\n{LINE}\n{trace}", file=sys.stderr)
            for description in reply["unexpected_successes"]:
                failed = True
                print(f"{'=' * 70}\nUNEXPECTED SUCCESS: {description} [{name}]", file=sys.stderr)
            if reply["output"]:
                print(f"{'=' * 70}\nOUTPUT: {test} [{name}]\n{LINE}\n{reply['output'].rstrip()}", file=sys.stderr)
            for text in reply["warnings"]:
                resource_warnings += ": ResourceWarning: " in text
                print(f"{'=' * 70}\nWARNING: {test} [{name}]\n{LINE}\n{text.rstrip()}", file=sys.stderr)
    for name, log in logs:
        log.seek(0)
        if text := log.read():
            resource_warnings += text.count(": ResourceWarning: ")
            print(f"{'=' * 70}\nWORKER ERROR OUTPUT [{name}]\n{LINE}\n{text.rstrip()}", file=sys.stderr)
        log.close()

    print(LINE, file=sys.stderr)
    durations = {}
    for name, replies in results.replies.items():
        for reply in replies.values():
            for test, seconds in reply["durations"].items():
                durations[test] = max(seconds, durations.get(test, 0))
    if slowest := [(test, seconds) for test, seconds in sorted(durations.items(), key=lambda item: -item[1])[:5]
                   if seconds >= 1]:
        print("Slowest: " + ", ".join(f"{test.rpartition('.')[2]} {seconds:.1f}s" for test, seconds in slowest),
              file=sys.stderr)
    skips = Counter(reason for replies in results.replies.values() for reply in replies.values()
                    for _, reason in reply["skipped"])
    for reason, count in skips.most_common():
        print(f"Skipped {count}: {reason}", file=sys.stderr)
    print(f"Ran {len(order)} tests under each of {len(PATHS)} PATHs in {elapsed:.1f}s, with {args.workers} workers per"
          " PATH", file=sys.stderr)
    for name, replies in results.replies.items():
        counts = {key: sum(len(reply[key]) for reply in replies.values())
                  for key in ["failures", "errors", "unexpected_successes", "skipped"]}
        summary = ", ".join(f"{key}={count}" for key, count in counts.items() if count)
        ok = not (counts["failures"] or counts["errors"] or counts["unexpected_successes"])
        print(f"{name}: {sum(reply['ran'] for reply in replies.values())} ran, {'OK' if ok else 'FAILED'}"
              f"{f' ({summary})' if summary else ''}", file=sys.stderr)
    print(f"ResourceWarnings: {resource_warnings}", file=sys.stderr)

    last.update(durations)
    with tempfile.NamedTemporaryFile("w", dir=DURATIONS.parent, prefix=DURATIONS.name, delete=False) as file:
        json.dump(last, file)
    os.replace(file.name, DURATIONS)
    sys.exit(1 if failed or resource_warnings else 0)


if __name__ == "__main__":
    work() if sys.argv[1:] == ["--worker"] else main()
