#!/usr/bin/env python3
"""Assert the CI additions for Linux removed nothing the other platforms rely on.

Compares the workflow's parsed job tree against a base revision rather than
grepping the diff text. A text guard has to maintain a list of literal
fragments that are allowed to disappear, and every prose rewrap or loop
rewrite makes that list wrong -- the version this replaced could not print
nothing even for a correct edit.

Usage: ci-nonregression.py [base-rev]
"""
import subprocess
import sys

import yaml

WORKFLOW = ".github/workflows/ci.yml"
FROZEN = ["diagnostics"]
KEEP_RUNNERS = ["macos-latest", "windows-latest"]


def load(rev):
    if rev is None:
        with open(WORKFLOW, encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    out = subprocess.run(["git", "show", "%s:%s" % (rev, WORKFLOW)],
                         capture_output=True, check=True, text=True).stdout
    return yaml.safe_load(out)


def step_names(job):
    return [s.get("name", s.get("uses", "?")) for s in job.get("steps", [])]


def main():
    base_rev = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    base, head = load(base_rev), load(None)
    rc = 0

    for name in FROZEN:
        if base["jobs"].get(name) != head["jobs"].get(name):
            print("FAIL  job %s changed; it is not part of Linux support" % name)
            rc = 1
        else:
            print("ok    job %s byte-identical" % name)

    for name, job in base["jobs"].items():
        if name not in head["jobs"]:
            print("FAIL  job %s was deleted" % name)
            rc = 1
            continue
        missing = [s for s in step_names(job)
                   if s not in step_names(head["jobs"][name])]
        if missing:
            print("FAIL  job %s lost step(s): %s" % (name, ", ".join(missing)))
            rc = 1
        else:
            print("ok    job %s kept all %d base step(s)"
                  % (name, len(step_names(job))))

    for name in ("test", "launcher"):
        runners = head["jobs"][name]["strategy"]["matrix"]["os"]
        for keep in KEEP_RUNNERS:
            if keep not in runners:
                print("FAIL  %s matrix dropped %s" % (name, keep))
                rc = 1
        if not any("ubuntu" in r for r in runners):
            print("FAIL  %s matrix has no ubuntu runner, but README promises "
                  "Linux CI" % name)
            rc = 1
        else:
            print("ok    %s matrix: %s" % (name, ", ".join(runners)))

    return rc


if __name__ == "__main__":
    sys.exit(main())
