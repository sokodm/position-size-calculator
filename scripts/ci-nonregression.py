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

Two later holes, both found by review, are closed the same way -- by deriving
the expectation from the base revision rather than writing it down here:

* A job's `runs-on` was never compared, so switching `launcher` from
  `${{ matrix.os }}` to a hardcoded `ubuntu-latest` passed with every step
  body and the matrix itself untouched -- silently dropping the Mac and
  Windows launcher runs, which is the exact regression this script exists to
  catch.
* The one step exempted from content comparison was checked with a list of
  literal substrings, so a rewrite that kept those strings as inert text while
  gutting the verification loop still reported "rewritten as planned". Both
  the variant split and the loop's machinery are now derived from the base
  body: the specs are parsed into (triple, file, variant) and the commands the
  step actually invokes are extracted by position, so a string that only
  appears as an argument no longer counts as the command being present.

Usage: ci-nonregression.py [base-rev]
"""
import re
import subprocess
import sys

import yaml

WORKFLOW = ".github/workflows/ci.yml"
FROZEN = ["diagnostics"]
KEEP_RUNNERS = ["macos-latest", "windows-latest"]
MATRIX_JOBS = ("test", "launcher")

# "aarch64-apple-darwin:$mac" in the base, "...:$mac:install_only" in the head.
SPEC_RE = re.compile(r'"([a-z0-9][a-z0-9_.]*(?:-[a-z0-9_.]+)+):\$(\w+)(?::(\w+))?"')
# The asset name the base builds, which is where its implicit variant lives.
ASSET_RE = re.compile(r'asset="[^"]*-\$triple-(\w+)\.tar\.gz"')
# Shell words that open a new command position rather than being a command.
SHELL_NOISE = frozenset((
    "for", "do", "done", "if", "then", "else", "elif", "fi", "in", "while",
    "case", "esac", "exit", "return",
))

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
        # checked_by names the derived comparison that stands in for content
        # equality here. It is deliberately not a list of literal fragments:
        # the base body is parsed for the triples it verifies and the variant
        # it builds, so nothing in this file has to be edited in lockstep with
        # a legitimate rewrite of the step, and a rewrite that keeps the old
        # strings as inert text does not pass.
        "checked_by": "hash_step",
    },
}


def commands(body):
    """Command names the body actually invokes, by position.

    Only the first word of each command position counts, so a gutted rewrite
    cannot keep `curl` or `awk` alive by mentioning them inside an argument or
    a string -- `echo "curl"` yields `echo`, not `curl`.
    """
    found = set()
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for seg in re.split(r"\|\||&&|[|;()`]|\$\(", line):
            word = re.match(r"([a-z][a-z0-9_.-]*)(=?)", seg.strip())
            # A trailing = makes it an assignment, not a command; the
            # names those hold are pinned by SPEC_RE instead.
            if word and not word.group(2) \
                    and word.group(1) not in SHELL_NOISE:
                found.add(word.group(1))
    return found


def hash_step(step_name, base_body, head_body):
    """The variant split and the verification machinery, both derived.

    Returns a list of failure lines; empty means the rewrite kept everything
    the base step guaranteed.
    """
    bad = []

    gone = sorted(commands(base_body) - commands(head_body))
    if gone:
        bad.append("step %r no longer runs: %s" % (step_name, ", ".join(gone)))

    base_variant = ASSET_RE.search(base_body)
    if not base_variant:
        bad.append("cannot read the base asset variant for step %r; this "
                   "check has gone stale and must be repaired, not skipped"
                   % step_name)
        return bad
    base_variant = base_variant.group(1)

    head_specs = dict((t, (f, v)) for t, f, v in SPEC_RE.findall(head_body))
    base_specs = SPEC_RE.findall(base_body)
    if not base_specs:
        bad.append("cannot read any triple out of the base step %r; this "
                   "check has gone stale and must be repaired, not skipped"
                   % step_name)
        return bad

    for triple, base_file, _ in base_specs:
        if triple not in head_specs:
            bad.append("step %r stopped verifying %s" % (step_name, triple))
            continue
        head_file, head_variant = head_specs[triple]
        if head_file != base_file:
            bad.append("%s moved from $%s to $%s" % (triple, base_file,
                                                     head_file))
        if head_variant and head_variant != base_variant:
            bad.append("%s switched from %s to %s; the base platforms keep "
                       "the unstripped build" % (triple, base_variant,
                                                 head_variant))
    return bad


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
        # A job that still has every step but a different runs-on runs those
        # steps somewhere else. Compared against the base rather than asserted
        # to equal a literal, so a legitimate future change to how the runner
        # is chosen fails here once and gets reviewed, instead of never.
        if head["jobs"][job_name].get("runs-on") != job.get("runs-on"):
            print("FAIL  job %s changed runs-on: %r -> %r"
                  % (job_name, job.get("runs-on"),
                     head["jobs"][job_name].get("runs-on")))
            rc = 1
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
                bad = globals()[spec["checked_by"]](
                    step_name, step.get("run", ""),
                    head_steps[step_name].get("run", ""))
                if bad:
                    for line in bad:
                        print("FAIL  %s" % line)
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

    for job_name in MATRIX_JOBS:
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
