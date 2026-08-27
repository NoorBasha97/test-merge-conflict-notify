#!/usr/bin/env python3
# managed-by: git-governance-workspace | capability: merge-conflict-notify | version: 1
"""
email-notification: a generic, provider-agnostic SMTP EmailService plus the
conflict/success email templates for the merge-conflict-notify workflow.

Vendored byte-for-byte into every governed repository's .governance-ci/
directory by conflict-workflow-installer (see merge-conflict-detection's
SKILL.md for why this repo carries the same file in two places). Stdlib
only (smtplib/email.mime) -- no vendor SMTP SDK, so any SMTP-capable
provider works, including delivery to Outlook addresses. Never imports
anything from `.claude/`.

Subcommand:
  send --to <comma-separated addresses> --subject <str> --body-file <path>
      Reads SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/SMTP_FROM_ADDRESS
      from the environment (never a file, never a CLI flag) and sends the
      email. Exit 0 ok, 2 on missing env/send failure. Useful for a local
      dry-run against e.g. `python3 -m smtpd -c DebuggingServer -n
      localhost:1025` (set SMTP_HOST=localhost SMTP_PORT=1025 and any
      placeholder user/password/from-address).
"""
import argparse
import os
import smtplib
import ssl
import sys
from email.mime.text import MIMEText


class EmailService:
    def __init__(self, host, port, user, password, from_address):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.from_address = from_address

    @classmethod
    def from_env(cls):
        """Reads SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/SMTP_FROM_ADDRESS
        from os.environ -- these arrive as masked CI/CD secrets at runtime,
        never hardcoded, never read from any file. Raises ValueError (with a
        clear message naming the missing variable) if any is absent --
        callers must catch this, log it, and continue rather than crash.
        """
        required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM_ADDRESS"]
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise ValueError(f"missing required SMTP env var(s): {', '.join(missing)}")
        return cls(
            host=os.environ["SMTP_HOST"],
            port=int(os.environ["SMTP_PORT"]),
            user=os.environ["SMTP_USER"],
            password=os.environ["SMTP_PASSWORD"],
            from_address=os.environ["SMTP_FROM_ADDRESS"],
        )

    def send(self, to_addresses, subject, body):
        """Plain smtplib.SMTP + STARTTLS. Raises on failure -- callers MUST
        catch, log, and continue rather than let this crash the whole
        workflow run (acceptance criterion: email provider failure is
        logged, never fatal).
        """
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = self.from_address
        msg["To"] = ", ".join(to_addresses)

        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(self.user, self.password)
            smtp.sendmail(self.from_address, to_addresses, msg.as_string())


def compose_conflict_email(repository, source_branch, targets, initiating_user, sha, pr_url):
    """targets: {target_branch: [conflicting_file, ...], ...}. Groups by
    target branch (section header, files listed under it) per the required
    format. Returns (subject, body).
    """
    subject = f"[{repository}] Merge conflict: {source_branch} -> " + ", ".join(sorted(targets.keys()))

    lines = [
        f"Repository: {repository}",
        f"Source branch: {source_branch}",
        f"Target branch(es): {', '.join(sorted(targets.keys()))}",
        "Conflict status: conflicts detected",
        f"Initiating user: {initiating_user}",
        f"Commit SHA: {sha}",
        f"PR/MR URL: {pr_url}",
        "",
        "Conflicting files:",
    ]
    for target_branch in sorted(targets.keys()):
        lines.append(f"  {target_branch}:")
        for path in targets[target_branch]:
            lines.append(f"    - {path}")
    lines += ["", "Action Required: Please review and resolve the merge conflicts."]
    return subject, "\n".join(lines)


def compose_success_email(repository, source_branch, target_branch, pr_url, merge_status, result_commit_sha):
    """Sent once an auto-merged, conflict-free PR/MR actually completes."""
    subject = f"[{repository}] Auto-merge complete: {source_branch} -> {target_branch}"
    lines = [
        f"Repository: {repository}",
        f"Source branch: {source_branch}",
        f"Target branch: {target_branch}",
        f"PR/MR URL: {pr_url}",
        f"Merge status: {merge_status}",
        f"Resulting commit: {result_commit_sha}",
    ]
    return subject, "\n".join(lines)


def cmd_send(args):
    try:
        service = EmailService.from_env()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)

    to_addresses = [a.strip() for a in args.to.split(",") if a.strip()]
    if not to_addresses:
        print("error: --to resolved to zero addresses", file=sys.stderr)
        sys.exit(2)

    with open(args.body_file) as f:
        body = f.read()

    try:
        service.send(to_addresses, args.subject, body)
    except Exception as exc:  # noqa: BLE001 -- any send failure must be reported, not crash the caller
        print(f"error: email send failed: {exc}", file=sys.stderr)
        sys.exit(2)

    print(f"sent to {len(to_addresses)} recipient(s)")
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Send an email via SMTP, configured entirely from the environment.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_send = sub.add_parser("send", help="Send an email via SMTP")
    p_send.add_argument("--to", required=True, help="comma-separated recipient addresses")
    p_send.add_argument("--subject", required=True)
    p_send.add_argument("--body-file", required=True)
    p_send.set_defaults(func=cmd_send)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
