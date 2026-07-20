#!/usr/bin/env python3
"""Strip user/member records from a Sentry export (relocation) JSON file.

A Sentry export is a JSON array of records, each shaped like:
    {"model": "sentry.user", "pk": 1, "fields": {...}}

This script drops every record whose "model" is in the removal set
(by default: sentry.user and sentry.organizationmember) and writes the
remainder to a new file with "_minus_members" inserted before ".json".

Usage:
    python strip_members.py export.json
    python strip_members.py export.json --models sentry.user sentry.organizationmember sentry.useremail
    python strip_members.py export.json -o /path/to/output.json
"""

import argparse
import json
import os
import re
import sys

# Models removed unless overridden with --models.
# Grouped by why they're removed; all hold or embed email addresses.
DEFAULT_MODELS = [
    # Email *is* the record.
    "sentry.user",                    # email, email_unique, username
    "sentry.useremail",               # email
    "sentry.email",                   # email
    "sentry.organizationmember",      # email (invite) + user_email
    "sentry.organizationmemberinvite",  # email
    "sentry.authidentity",            # ident (SSO identity, ~always an email)
    # Config records that embed emails in generic text fields.
    "sentry.alertruletriggeraction",  # target_identifier / target_display
    "sentry.notificationaction",      # target_identifier / target_display
    "sentry.projectownership",        # raw (ownership rules with emails)
]

# Matches typical email addresses for scrubbing and the safety scan.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Every email found in the kept records is replaced with this.
DEFAULT_PLACEHOLDER = "myemail@example.com"


def scrub_emails(value, placeholder):
    """Recursively replace every email-like string inside a JSON value
    (dict/list/str) with the placeholder. Returns (new_value, count)."""
    if isinstance(value, str):
        # Don't count the placeholder replacing itself.
        new_value, n = EMAIL_RE.subn(
            lambda m: m.group(0) if m.group(0) == placeholder else placeholder,
            value,
        )
        return new_value, sum(1 for _ in EMAIL_RE.finditer(value)
                              if _.group(0) != placeholder)
    if isinstance(value, list):
        count = 0
        out = []
        for item in value:
            new_item, n = scrub_emails(item, placeholder)
            out.append(new_item)
            count += n
        return out, count
    if isinstance(value, dict):
        count = 0
        out = {}
        for k, v in value.items():
            new_v, n = scrub_emails(v, placeholder)
            out[k] = new_v
            count += n
        return out, count
    return value, 0


def scan_for_emails(records, placeholder):
    """Return {model: count} for records whose serialized fields still
    contain a real (non-placeholder) email-like string."""
    hits = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        blob = json.dumps(record.get("fields", record))
        real = [m for m in EMAIL_RE.findall(blob) if m != placeholder]
        if real:
            model = record.get("model", "<unknown>")
            hits[model] = hits.get(model, 0) + len(real)
    return hits


def default_output_path(input_path: str) -> str:
    """Insert '_minus_members' before the .json extension."""
    root, ext = os.path.splitext(input_path)
    if ext.lower() != ".json":
        # No .json extension: just append the suffix.
        return input_path + "_minus_members"
    return f"{root}_minus_members{ext}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="Path to the export JSON file")
    parser.add_argument("-o", "--output",
                        help="Output path (default: <input>_minus_members.json)")
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS,
                        help=f"Model names to remove (default: {' '.join(DEFAULT_MODELS)})")
    parser.add_argument("--placeholder", default=DEFAULT_PLACEHOLDER,
                        help=f"Email to substitute for any survivor (default: {DEFAULT_PLACEHOLDER})")
    parser.add_argument("--no-scrub", action="store_true",
                        help="Skip the email-scrub pass (only remove records)")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"Error: file not found: {args.input}", file=sys.stderr)
        return 1

    try:
        with open(args.input, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: {args.input} is not valid JSON: {e}", file=sys.stderr)
        return 1

    if not isinstance(data, list):
        print("Error: expected the export to be a JSON array of records.",
              file=sys.stderr)
        return 1

    remove = set(args.models)
    kept, removed_counts = [], {}
    for record in data:
        model = record.get("model") if isinstance(record, dict) else None
        if model in remove:
            removed_counts[model] = removed_counts.get(model, 0) + 1
        else:
            kept.append(record)

    # Safety-net scrub: replace any email surviving in the kept records.
    scrubbed_count = 0
    if not args.no_scrub:
        kept, scrubbed_count = scrub_emails(kept, args.placeholder)

    output_path = args.output or default_output_path(args.input)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(kept, f, indent=2)

    total_removed = sum(removed_counts.values())
    print(f"Read {len(data)} records from {args.input}")
    if removed_counts:
        for model in sorted(removed_counts):
            print(f"  removed {removed_counts[model]} x {model}")
    else:
        print(f"  removed 0 (no records matched: {', '.join(sorted(remove))})")
    print(f"Wrote {len(kept)} records ({total_removed} removed) to {output_path}")
    if not args.no_scrub:
        print(f"Scrubbed {scrubbed_count} email(s) -> {args.placeholder}")

    # Safety net: warn if any real (non-placeholder) email survives.
    remaining = scan_for_emails(kept, args.placeholder)
    if remaining:
        print("\nWARNING: real email-like strings still present in the output:",
              file=sys.stderr)
        for model in sorted(remaining):
            print(f"  {remaining[model]} in {model}", file=sys.stderr)
    else:
        print("Email scan: no real email addresses remain in the output.")
    return 0


import os as _rl_os, sys as _rl_sys
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "..", "common"))
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "common"))
from run_logging import start_run_log


if __name__ == "__main__":
    start_run_log("strip_members")
    sys.exit(main())
