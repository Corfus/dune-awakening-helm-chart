#!/usr/bin/env python3
"""Bump, commit, tag and push a chart release; GitLab CI validates and publishes it."""

import argparse
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


def run(*args, capture=False):
    result = subprocess.run(args, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bump", choices=("patch", "minor", "major"))
    parser.add_argument("--remote", default="origin", help="Git remote (default: origin)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changing files or pushing")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    # Resolve paths from the script so it also works outside the chart directory.
    os.chdir(root)
    if run("git", "status", "--porcelain", capture=True):
        raise ValueError("Commit or stash existing changes before releasing.")
    branch = run("git", "symbolic-ref", "--quiet", "--short", "HEAD", capture=True)
    run("git", "remote", "get-url", args.remote, capture=True)

    chart = root / "Chart.yaml"
    original = chart.read_text()
    match = re.search(
        r"^(version:[ \t]*)([\"']?)((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))\2([ \t]*(?:#.*)?)$",
        original, re.MULTILINE,
    )
    if not match:
        raise ValueError("Chart.yaml must have a stable version in MAJOR.MINOR.PATCH format.")
    major, minor, patch = map(int, match[3].split("."))
    version = {
        "patch": f"{major}.{minor}.{patch + 1}",
        "minor": f"{major}.{minor + 1}.0",
        "major": f"{major + 1}.0.0",
    }[args.bump]
    tag = f"v{version}"
    print(f"Release {match[3]} -> {version} on {branch}; remote {args.remote}", flush=True)
    if args.dry_run:
        print(f"Would update Chart.yaml, commit, tag {tag}, "
              "and atomically push the branch and tag.")
        return

    for candidate in (tag, version):
        if run("git", "tag", "--list", candidate, capture=True):
            raise ValueError(f"Local release tag {candidate} already exists.")
    if run("git", "ls-remote", "--tags", args.remote,
           f"refs/tags/{tag}", f"refs/tags/{version}", capture=True):
        raise ValueError(f"Release {version} already exists on {args.remote}.")

    chart.write_text(original[:match.start(3)] + version + original[match.end(3):])
    run("git", "add", "--", "Chart.yaml")
    run("git", "commit", "-m", f"Release chart {version}", "--", "Chart.yaml")
    run("git", "tag", "-a", tag, "-m", f"Release chart {version}")
    print(f"Pushing {branch} and {tag}. If the push fails, the local commit and tag "
          "remain available; fix the cause and retry the printed command.", flush=True)
    push = ("git", "push", "--atomic", args.remote,
            f"HEAD:refs/heads/{branch}", f"refs/tags/{tag}")
    print(shlex.join(push), flush=True)
    run(*push)
    print(f"Published Git tag {tag}; GitLab CI will test and publish the Helm chart.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as error:
        print(f"Release failed: {error}", file=sys.stderr)
        sys.exit(1)
