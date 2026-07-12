#!/usr/bin/env python3
"""
Backfill a "Created By" single-select field on a GitHub Projects v2 board.

GitHub Projects has no native author/created-by filter (and reserves "Author"
as a field name). The workaround is a custom field populated from the issue's
author. A workflow handles new issues; this script handles everything already
on the board.

Dry-run by default. Nothing is written unless you pass --apply.

Usage:
    export GITHUB_TOKEN=github_pat_...        # org Projects: read/write, Issues: read
    python backfill_project_author.py --owner freshabilityapp --project 1
    python backfill_project_author.py --owner freshabilityapp --project 1 --apply

Requires: pip install requests
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from typing import Any

import requests

API_URL = "https://api.github.com/graphql"
PAGE_SIZE = 100

# Colors cycled through when auto-creating options.
OPTION_COLORS = ["BLUE", "GREEN", "YELLOW", "ORANGE", "RED", "PINK", "PURPLE", "GRAY"]


class GraphQLError(RuntimeError):
    pass


def gql(session: requests.Session, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """POST a GraphQL query, retrying on transient failures and rate limits."""
    for attempt in range(5):
        resp = session.post(API_URL, json={"query": query, "variables": variables}, timeout=30)

        if resp.status_code in (502, 503, 504):
            time.sleep(2**attempt)
            continue

        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            reset = int(resp.headers.get("x-ratelimit-reset", time.time() + 60))
            wait = max(reset - int(time.time()), 1) + 1
            print(f"  rate limited; sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue

        resp.raise_for_status()
        payload = resp.json()

        if "errors" in payload:
            raise GraphQLError("; ".join(e.get("message", str(e)) for e in payload["errors"]))
        return payload["data"]

    raise GraphQLError("giving up after 5 attempts")


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #

PROJECT_FIELD_Q = """
query($owner: String!, $number: Int!, $field: String!) {
  organization(login: $owner) {
    projectV2(number: $number) {
      id
      title
      field(name: $field) {
        ... on ProjectV2SingleSelectField {
          id
          options { id name }
        }
      }
    }
  }
}
"""

ITEMS_Q = """
query($project: ID!, $cursor: String, $field: String!) {
  node(id: $project) {
    ... on ProjectV2 {
      items(first: %d, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          type
          fieldValueByName(name: $field) {
            ... on ProjectV2ItemFieldSingleSelectValue { name optionId }
          }
          content {
            ... on Issue {
              number
              author { login }
              repository { nameWithOwner }
            }
          }
        }
      }
    }
  }
}
""" % PAGE_SIZE

SET_FIELD_M = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project
    itemId: $item
    fieldId: $field
    value: { singleSelectOptionId: $option }
  }) { projectV2Item { id } }
}
"""

# NOTE: this mutation REPLACES the whole option list, so we always resend the
# existing options plus the new ones. Existing option IDs are preserved when
# the option name is unchanged.
UPDATE_OPTIONS_M = """
mutation($field: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
  updateProjectV2Field(input: { fieldId: $field, singleSelectOptions: $options }) {
    projectV2Field {
      ... on ProjectV2SingleSelectField {
        id
        options { id name }
      }
    }
  }
}
"""


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #

def fetch_field(session, owner: str, number: int, field_name: str):
    data = gql(session, PROJECT_FIELD_Q, {"owner": owner, "number": number, "field": field_name})
    project = (data.get("organization") or {}).get("projectV2")
    if not project:
        sys.exit(f"No project #{number} under org '{owner}' (or the token can't see it).")

    field = project.get("field")
    if not field or "options" not in field:
        sys.exit(f"No single-select field named '{field_name}' on '{project['title']}'.")

    options = {o["name"]: o["id"] for o in field["options"]}
    print(f"Project: {project['title']}  |  field '{field_name}' with {len(options)} option(s)")
    return project["id"], field["id"], options


def fetch_items(session, project_id: str, field_name: str):
    """Yield every ISSUE item on the board."""
    cursor = None
    while True:
        data = gql(session, ITEMS_Q, {"project": project_id, "cursor": cursor, "field": field_name})
        page = data["node"]["items"]
        for node in page["nodes"]:
            if node["type"] == "ISSUE" and node.get("content"):
                yield node
        if not page["pageInfo"]["hasNextPage"]:
            return
        cursor = page["pageInfo"]["endCursor"]


def create_missing_options(session, field_id: str, options: dict[str, str], missing: list[str]):
    """Append new options, preserving the existing ones."""
    payload = [
        {"name": name, "description": "", "color": OPTION_COLORS[i % len(OPTION_COLORS)]}
        for i, name in enumerate(list(options) + sorted(missing))
    ]
    data = gql(session, UPDATE_OPTIONS_M, {"field": field_id, "options": payload})
    refreshed = data["updateProjectV2Field"]["projectV2Field"]["options"]
    print(f"Created {len(missing)} option(s): {', '.join(sorted(missing))}")
    return {o["name"]: o["id"] for o in refreshed}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--owner", required=True, help="Org login that owns the project")
    ap.add_argument("--project", type=int, required=True, help="Project number from its URL")
    ap.add_argument("--field", default="Created By",
                    help='Single-select field name (default: "Created By" — "Author" is reserved by GitHub)')
    ap.add_argument("--apply", action="store_true", help="Actually write. Omit for a dry run.")
    ap.add_argument("--force", action="store_true", help="Overwrite items that already have a value")
    ap.add_argument("--create-missing-options", action="store_true",
                    help="Auto-add options for unmapped author logins")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("Set GITHUB_TOKEN (org Projects read/write + Issues read).")

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })

    project_id, field_id, options = fetch_field(session, args.owner, args.project, args.field)

    print("Scanning items...")
    items = list(fetch_items(session, project_id, args.field))
    print(f"{len(items)} issue item(s) on the board.\n")

    # Pass 1: classify.
    todo, unmapped, skipped = [], Counter(), 0
    for item in items:
        content = item["content"]
        login = (content.get("author") or {}).get("login")
        current = item.get("fieldValueByName")

        if not login:            # deleted account / ghost author
            continue
        if current and not args.force:
            skipped += 1
            continue
        if login not in options:
            unmapped[login] += 1
            continue
        todo.append((item, content, login))

    # Pass 2: optionally create the missing options, then re-classify those items.
    if unmapped and args.create_missing_options:
        if args.apply:
            options = create_missing_options(session, field_id, options, list(unmapped))
            for item in items:
                content = item["content"]
                login = (content.get("author") or {}).get("login")
                current = item.get("fieldValueByName")
                if login in unmapped and (args.force or not current):
                    todo.append((item, content, login))
            unmapped.clear()
        else:
            print(f"[dry run] would create option(s): {', '.join(sorted(unmapped))}\n")

    # Pass 3: write.
    verb = "Setting" if args.apply else "[dry run] would set"
    updated, failed = 0, 0
    for item, content, login in todo:
        ref = f"{content['repository']['nameWithOwner']}#{content['number']}"
        print(f"{verb} {args.field}='{login}' on {ref}")
        if not args.apply:
            continue
        try:
            gql(session, SET_FIELD_M, {
                "project": project_id,
                "item": item["id"],
                "field": field_id,
                "option": options[login],
            })
            updated += 1
        except (GraphQLError, requests.HTTPError) as exc:
            print(f"  FAILED {ref}: {exc}", file=sys.stderr)
            failed += 1

    # Summary.
    print("\n--- summary ---")
    print(f"already set (skipped): {skipped}")
    print(f"{'updated' if args.apply else 'would update'}: {updated if args.apply else len(todo)}")
    if failed:
        print(f"failed: {failed}")
    if unmapped:
        print(f"\nNo '{args.field}' option for these {len(unmapped)} login(s):")
        for login, count in unmapped.most_common():
            print(f"  {login:<24} {count} issue(s)")
        print("\nAdd them to the project field (or rerun with --create-missing-options), then rerun.")
    if not args.apply:
        print("\nDry run — nothing was written. Rerun with --apply.")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
