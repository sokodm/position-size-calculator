#!/usr/bin/env python3
"""Assert the CI additions for Linux removed nothing the other platforms rely on.

Compares the workflow's parsed job tree against a base revision rather than
grepping the diff text. A text guard has to maintain a list of literal
fragments that are allowed to disappear, and every prose rewrap or loop
rewrite makes that list wrong -- the version this replaced could not print
nothing even for a correct edit.

Steps are compared by full content, not by name. An earlier version of this
script checked only that each base step's name still existed, and a review
proved it passed with exit 0 after the entire body of a macOS step had been
replaced by `echo gutted` -- a step neutered in place is at least as likely a
regression as one deleted outright.

Usage: ci-nonregression.py [base-rev]
"""
import subprocess
import sys

import yaml

WORKFLOW = ".github/workflows/ci.yml"
FROZEN = ["diagnostics"]
KEEP_RUNNERS = ["macos-latest", "windows-latest"]

# Base steps Linux support is expected to rewrite, by name, with the reason.
# Anything else changing in place is a finding. Keyed on step name because a
# name is a stable identifier; if one of these is renamed or deleted the check
# fails rather than silently skipping, which is the safe direction.
EXPECTED_CHANGES = {
    "Pinned Python hashes still match the published release": {
        "why": "carries the asset variant per triple now: Linux pins "
               "install_only_stripped while Mac and Windows keep install_only",
        # Exempting a step from content comparison exempts whatever invariant
        # it encodes -- and this is the step that encodes the variant split.
        # Without these, Windows could be switched to install_only_stripped
        # inside the rewrite and nothing here would notice.
        "must_contain": [
            '"aarch64-apple-darwin:$mac:install_only"',
            '"x86_64-apple-darwin:$mac:install_only"',
            '"x86_64-pc-windows-msvc:$win:install_only"',
            "x86_64-unknown-linux-gnu:$lin:install_only_stripped",
            "aarch64-unknown-linux-gnu:$lin:install_only_stripped",
        ],
    },
}


def load(rev):
    if rev is None:
        with open(WORKFLOW, encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    out = subprocess.run(["git", "show", "%s:%s" % (rev, WORKFLOW)],
                         capture_output=True, check=True, text=True).stdout
    return yaml.safe_load(out)


def by_name(job):
    return dict((s.get("name", s.get("uses", "?")), s)
                for s in job.get("steps", []))


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

    for job_name, job in base["jobs"].items():
        if job_name not in head["jobs"]:
            print("FAIL  job %s was deleted" % job_name)
            rc = 1
            continue
        head_steps = by_name(head["jobs"][job_name])
        kept = changed = 0
        for step_name, step in by_name(job).items():
            if step_name not in head_steps:
                print("FAIL  job %s lost step: %s" % (job_name, step_name))
                rc = 1
            elif head_steps[step_name] == step:
                kept += 1
            elif step_name in EXPECTED_CHANGES:
                spec = EXPECTED_CHANGES[step_name]
                body = head_steps[step_name].get("run", "")
                lost = [m for m in spec.get("must_contain", [])
                        if m not in body]
                if lost:
                    for m in lost:
                        print("FAIL  step %r no longer carries: %s"
                              % (step_name, m))
                    rc = 1
                else:
                    print("ok    step rewritten as planned: %s\n        (%s)"
                          % (step_name, spec["why"]))
                    changed += 1
            else:
                print("FAIL  job %s changed step in place: %s"
                      % (job_name, step_name))
                rc = 1
        print("ok    job %s kept %d base step(s) unchanged%s"
              % (job_name, kept,
                 ", %d rewritten as planned" % changed if changed else ""))

    for job_name in ("test", "launcher"):
        runners = head["jobs"][job_name]["strategy"]["matrix"]["os"]
        for keep in KEEP_RUNNERS:
            if keep not in runners:
                print("FAIL  %s matrix dropped %s" % (job_name, keep))
                rc = 1
        if not any("ubuntu" in r for r in runners):
            print("FAIL  %s matrix has no ubuntu runner, but README promises "
                  "Linux CI" % job_name)
            rc = 1
        else:
            print("ok    %s matrix: %s" % (job_name, ", ".join(runners)))

    return rc


if __name__ == "__main__":
    sys.exit(main())
