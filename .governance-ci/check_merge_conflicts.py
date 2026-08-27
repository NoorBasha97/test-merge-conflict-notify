#!/usr/bin/env python3
# managed-by: git-governance-workspace | capability: merge-conflict-notify | version: 1
"""
Standalone orchestrator for the merge-conflict-notify workflow. Executed by
the installed GitHub Actions / GitLab CI job. Imports its two vendored
siblings (conflict_detection.py, email_service.py) from the same
.governance-ci/ directory -- never imports anything from .claude/, which
does not exist in this repository.

Every value is read from the environment: GOVERNANCE_* variables are either
literals baked into the installed workflow file at install time (never
sensitive: default branch, auto_merge, conflict-email-enabled) or mapped 1:1
from the CI provider's own ambient trigger context for this run (PR/MR
number, branches, actor, SHA, action) -- normalized to the same GOVERNANCE_*
names by both the GitHub Actions and GitLab CI YAML, so this script needs no
provider-specific branching beyond which detection/merge functions it
calls. REVIEWER_EMAILS and SMTP_* arrive as masked provider secrets/
variables, never as plaintext anywhere in this repository.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conflict_detection import detect, github_conflicting_files, gitlab_conflicting_files  # noqa: E402
from email_service import EmailService, compose_conflict_email, compose_success_email  # noqa: E402


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() == "true"


def reviewer_emails():
    return [e.strip() for e in os.environ.get("REVIEWER_EMAILS", "").split(",") if e.strip()]


def send_safely(to_addresses, subject, body):
    try:
        EmailService.from_env().send(to_addresses, subject, body)
        print(f"sent email to {len(to_addresses)} recipient(s)")
    except Exception as exc:  # noqa: BLE001 -- must never crash the workflow run
        print(f"email send failed (logged, not fatal): {exc}")


def handle_conflict(provider, repo, pr_number, target_branch, actor, sha, pr_url):
    if not env_bool("CONFLICT_EMAIL_ENABLED", True):
        print("conflict-email notification disabled for this repo -- logging only, not sending.")
        return
    emails = reviewer_emails()
    if not emails:
        print("no reviewer emails configured -- logging only, not sending, not failing the run.")
        return

    try:
        if provider == "github":
            files = github_conflicting_files(target_branch)
        else:
            files = gitlab_conflicting_files(repo, pr_number)
    except RuntimeError as exc:
        print(f"conflicting-file detection failed (reporting conflict with an empty file list): {exc}")
        files = []

    subject, body = compose_conflict_email(
        repository=repo, source_branch=os.environ.get("GOVERNANCE_SOURCE_BRANCH", "unknown"),
        targets={target_branch: files}, initiating_user=actor, sha=sha, pr_url=pr_url,
    )
    send_safely(emails, subject, body)


def enable_native_auto_merge(provider, repo, pr_number):
    try:
        if provider == "github":
            subprocess.run(["gh", "pr", "merge", str(pr_number), "--auto", "--merge", "--repo", repo], check=False)
        else:
            subprocess.run(["glab", "mr", "merge", str(pr_number), "--auto-merge",
                             "--when-pipeline-succeeds", "--repo", repo], check=False)
    except FileNotFoundError as exc:
        print(f"could not invoke provider CLI to enable auto-merge (logged, not fatal): {exc}")


def handle_no_conflict(provider, repo, pr_number):
    if not env_bool("GOVERNANCE_AUTO_MERGE", False):
        print("auto_merge is disabled for this repo -- no action taken, no email sent.")
        return
    print("no conflicts and auto_merge is enabled -- enabling native auto-merge (existing governance still applies).")
    enable_native_auto_merge(provider, repo, pr_number)


def handle_merge_completion(provider, repo, pr_number, target_branch, pr_url):
    """Only reachable from the second (closed/merged, or GitLab's best-effort
    push-to-target-branch) trigger. Silently does nothing if auto_merge is
    off or no reviewer email is configured -- by design, not an error."""
    if not env_bool("GOVERNANCE_AUTO_MERGE", False):
        return
    emails = reviewer_emails()
    if not emails:
        print("no reviewer emails configured -- skipping success email.")
        return
    sha = os.environ.get("GOVERNANCE_SHA", "unknown")
    subject, body = compose_success_email(
        repository=repo, source_branch=os.environ.get("GOVERNANCE_SOURCE_BRANCH", "unknown"),
        target_branch=target_branch, pr_url=pr_url, merge_status="merged", result_commit_sha=sha,
    )
    send_safely(emails, subject, body)


def main():
    provider = os.environ["GOVERNANCE_PROVIDER"]
    default_branch = os.environ["GOVERNANCE_DEFAULT_BRANCH"]
    source_branch = os.environ.get("GOVERNANCE_SOURCE_BRANCH", "")
    target_branch = os.environ.get("GOVERNANCE_TARGET_BRANCH", "")
    repo = os.environ["GOVERNANCE_REPO"]
    pr_number = os.environ.get("GOVERNANCE_PR_NUMBER", "0")
    actor = os.environ.get("GOVERNANCE_ACTOR", "unknown")
    sha = os.environ.get("GOVERNANCE_SHA", "unknown")
    pr_url = os.environ.get("GOVERNANCE_PR_URL", "")
    action = os.environ.get("GOVERNANCE_ACTION", "open_or_sync")

    if target_branch and target_branch != default_branch:
        print(f"target branch '{target_branch}' is not the default branch '{default_branch}' -- skipping.")
        return

    if action == "closed":
        if env_bool("GOVERNANCE_MERGED", False):
            handle_merge_completion(provider, repo, pr_number, target_branch, pr_url)
        else:
            print("PR/MR closed without merging -- no action.")
        return

    result = detect(provider, repo=repo, pr_number=int(pr_number))
    if result.status == "unknown":
        print("mergeability still unresolved after bounded retries -- taking no action, not assuming clean.")
        return
    if result.status == "conflict":
        handle_conflict(provider, repo, int(pr_number), target_branch, actor, sha, pr_url)
        return
    handle_no_conflict(provider, repo, int(pr_number))


if __name__ == "__main__":
    main()
