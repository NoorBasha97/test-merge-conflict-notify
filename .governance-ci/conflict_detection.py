#!/usr/bin/env python3
# managed-by: git-governance-workspace | capability: merge-conflict-notify | version: 1
"""
merge-conflict-detection: polls a provider's mergeability field for a PR/MR
until a definitive result is returned, and (where possible non-destructively)
identifies the exact conflicting file paths.

This file is vendored byte-for-byte into every governed repository's
.governance-ci/ directory by conflict-workflow-installer -- it is the single
source of truth for this logic, imported both here (for local/mocked unit
testing via the `detect` CLI subcommand below) and by the vendored runtime
orchestrator check_merge_conflicts.py in the target repo's own CI, which has
no access to this control-plane repo at all. Stdlib only -- no pip install
step needed on either side; never import anything from `.claude/`.

Subcommand:
  detect --provider {github,gitlab} --repo <owner/repo|project-id> --pr-number <n>
         [--target-branch <branch>] [--max-attempts 10] [--backoff-seconds 3.0]
      Poll mergeability and print a MergeabilityResult as JSON. Exit 0
      regardless of the detected status (conflict/clean/unknown are all valid
      outcomes, not errors) -- exit 2 only on a hard provider-API failure.
"""
import argparse
import json
import subprocess
import sys
import time


class MergeabilityResult:
    def __init__(self, status, conflicting_files=None, sha=None):
        self.status = status  # "clean" | "conflict" | "unknown"
        self.conflicting_files = conflicting_files or []
        self.sha = sha

    def to_dict(self):
        return {"status": self.status, "conflicting_files": self.conflicting_files, "sha": self.sha}


def run_gh(*args):
    """subprocess to `gh api <args>`, parses JSON stdout. Raises
    RuntimeError with the captured stderr on non-zero exit -- callers decide
    whether that's fatal; the poller below treats a transient failure as
    "try again next attempt", not an immediate hard stop.
    """
    proc = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args)} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def run_glab(*args):
    proc = subprocess.run(["glab", "api", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"glab api {' '.join(args)} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def run_git(*args, check=True):
    """Local git command, never touches the remote -- used only for the
    GitHub conflicting-files check below (a local `git merge --no-commit` in
    the runner's own ephemeral checkout, then aborted; nothing is pushed or
    committed anywhere).
    """
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def poll_github_mergeability(owner_repo, pr_number, max_attempts=10, backoff_seconds=3.0):
    """GitHub computes `mergeable` asynchronously -- it is frequently null
    immediately after PR creation/update. Poll (with linear backoff) until
    it resolves to a real boolean, or exhaust max_attempts and report
    "unknown" -- an unresolved mergeability must never be silently coerced
    to "clean".
    """
    last_error = None
    for attempt in range(max_attempts):
        try:
            pr = run_gh(f"repos/{owner_repo}/pulls/{pr_number}")
        except RuntimeError as exc:
            last_error = exc
            if attempt < max_attempts - 1:
                time.sleep(backoff_seconds * (attempt + 1))
            continue
        mergeable = pr.get("mergeable")
        if mergeable is not None:
            status = "clean" if mergeable else "conflict"
            return MergeabilityResult(status, sha=pr.get("head", {}).get("sha"))
        if attempt < max_attempts - 1:
            time.sleep(backoff_seconds * (attempt + 1))
    if last_error is not None:
        print(f"mergeability polling exhausted retries, last error: {last_error}", file=sys.stderr)
    return MergeabilityResult("unknown")


def github_conflicting_files(target_branch):
    """Non-destructive local check: attempt to merge `target_branch` into
    the current checkout (the PR head, already checked out by the calling
    workflow's actions/checkout step) with --no-commit --no-ff, read the
    conflicted paths from `git diff --diff-filter=U`, then abort. Nothing is
    pushed or committed anywhere -- this stays entirely inside the runner's
    own ephemeral workspace, unlike a merge-tree write via the REST API
    (which would create a real commit/ref and was rejected as an approach).
    Returns [] if the merge actually completes cleanly (a safe no-op; the
    caller should only reach here when mergeability already reported
    "conflict").
    """
    run_git("fetch", "origin", target_branch, check=False)
    merge = run_git("merge", "--no-commit", "--no-ff", f"origin/{target_branch}", check=False)
    try:
        if merge.returncode == 0:
            return []
        diff = run_git("diff", "--name-only", "--diff-filter=U")
        return [line for line in diff.stdout.splitlines() if line]
    finally:
        run_git("merge", "--abort", check=False)


def poll_gitlab_mergeability(project_id, mr_iid, max_attempts=10, backoff_seconds=3.0):
    """GitLab's merge_status/detailed_merge_status is also not always
    immediately authoritative -- force a recheck first, then poll until the
    status leaves the "still computing" states ("checking"/"unchecked"/
    "preparing"). Anything else that isn't "conflict" is treated as clean:
    approvals/required-CI status are separate concerns this workflow does
    not evaluate (they're already governed elsewhere, see Principle 8).
    """
    try:
        run_glab(f"projects/{project_id}/merge_requests/{mr_iid}/merge_status_recheck", "-X", "PUT")
    except RuntimeError as exc:
        print(f"merge_status_recheck failed (continuing to poll anyway): {exc}", file=sys.stderr)

    computing_states = {"checking", "unchecked", "preparing"}
    last_error = None
    for attempt in range(max_attempts):
        try:
            mr = run_glab(f"projects/{project_id}/merge_requests/{mr_iid}")
        except RuntimeError as exc:
            last_error = exc
            if attempt < max_attempts - 1:
                time.sleep(backoff_seconds * (attempt + 1))
            continue
        status = mr.get("detailed_merge_status") or mr.get("merge_status")
        if status and status not in computing_states:
            result_status = "conflict" if status == "conflict" else "clean"
            return MergeabilityResult(result_status, sha=mr.get("sha"))
        if attempt < max_attempts - 1:
            time.sleep(backoff_seconds * (attempt + 1))
    if last_error is not None:
        print(f"mergeability polling exhausted retries, last error: {last_error}", file=sys.stderr)
    return MergeabilityResult("unknown")


def gitlab_conflicting_files(project_id, mr_iid):
    """GitLab exposes this directly -- a real asymmetry with GitHub, where no
    endpoint returns the conflicting subset without a destructive merge-tree
    write. Disclosed here rather than worked around, matching this control
    plane's existing precedent of disclosing GitHub/GitLab asymmetries
    rather than faking parity.
    """
    changes = run_glab(f"projects/{project_id}/merge_requests/{mr_iid}/changes")
    if not changes.get("has_conflicts"):
        return []
    return [c["new_path"] for c in changes.get("changes", []) if c.get("new_path")]


def detect(provider, **kwargs):
    """Single dispatch entrypoint the vendored orchestrator calls."""
    if provider == "github":
        return poll_github_mergeability(
            kwargs["repo"], kwargs["pr_number"],
            kwargs.get("max_attempts", 10), kwargs.get("backoff_seconds", 3.0),
        )
    if provider == "gitlab":
        return poll_gitlab_mergeability(
            kwargs["repo"], kwargs["pr_number"],
            kwargs.get("max_attempts", 10), kwargs.get("backoff_seconds", 3.0),
        )
    raise ValueError(f"unknown provider '{provider}'")


def cmd_detect(args):
    try:
        result = detect(
            args.provider, repo=args.repo, pr_number=args.pr_number,
            max_attempts=args.max_attempts, backoff_seconds=args.backoff_seconds,
        )
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(2)

    if result.status == "conflict":
        try:
            if args.provider == "github" and args.target_branch:
                result.conflicting_files = github_conflicting_files(args.target_branch)
            elif args.provider == "gitlab":
                result.conflicting_files = gitlab_conflicting_files(args.repo, args.pr_number)
        except RuntimeError as exc:
            print(json.dumps({"warning": f"conflicting-file detection failed: {exc}"}), file=sys.stderr)

    print(json.dumps(result.to_dict()))
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Poll a PR/MR's mergeability and identify conflicting files.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_detect = sub.add_parser("detect", help="Poll mergeability and report conflicting files")
    p_detect.add_argument("--provider", required=True, choices=["github", "gitlab"])
    p_detect.add_argument("--repo", required=True, help="owner/repo for GitHub, numeric project id for GitLab")
    p_detect.add_argument("--pr-number", required=True, type=int, dest="pr_number")
    p_detect.add_argument("--target-branch", default=None, help="required for GitHub conflicting-file detection (local merge check)")
    p_detect.add_argument("--max-attempts", type=int, default=10)
    p_detect.add_argument("--backoff-seconds", type=float, default=3.0)
    p_detect.set_defaults(func=cmd_detect)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
