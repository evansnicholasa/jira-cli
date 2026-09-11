#!/usr/bin/env python3
"""A small Jira Cloud CLI, so anything with a shell can read and write a board.

**Why this exists.** The board this was written for was driven through
Atlassian's hosted Rovo MCP server, which meters against Rovo credits — and
Rovo is not offered on the Free plan. Rather than pay for a tier to keep one
integration alive, this talks to the Jira Cloud REST API directly, which every
plan including Free provides with no metering.

**Why a CLI rather than an MCP server.** It runs anywhere there is a shell, so
the same file serves an editor, a terminal, a CI job and more than one AI coding
agent. An MCP server only works where it has been configured. A file in a repo
is also reviewable and testable in a way a hosted service is not.

**Why API v2 rather than v3.** v3 requires Atlassian Document Format — a JSON
tree — for descriptions and comments, so writing a ticket with headings, tables
and code blocks would mean building a markdown-to-ADF converter first. v2 takes
plain text with wiki markup, which expresses all of that directly: `h2.`
headings, `*bold*`, `{code}` blocks, `||header||` tables. It is better on the
way back too, returning readable markup where v3 returns ADF JSON that has to
be walked to be read at all.

The cost is that v3 is the version Atlassian calls current. Both have been live
for years; if v2 is ever withdrawn, the ADF converter is a known, bounded piece
of follow-up rather than a surprise.

**Why stdlib only.** No `requests`, no virtualenv, no install step — so the
same file runs identically wherever it lands, on whatever Python is on PATH.
That is not an aesthetic preference: a tool kept for occasional use is one whose
dependencies rot between uses, and the first run after six months should not
start with a resolver error.

Credentials come from the environment, or from a file outside the repo:

    JIRA_SITE       your-site.atlassian.net
    JIRA_EMAIL      the Atlassian account email
    JIRA_API_TOKEN  from id.atlassian.com/manage-profile/security/api-tokens
    JIRA_PROJECT    optional; the default project key for `create`

Usage. Spelled with an explicit interpreter rather than relying on the shebang
below: `python3` in Git Bash on Windows resolves to the Microsoft Store's stub,
which advertises the Store instead of running anything. The shebang is for the
Unix side.

    python jira.py get PROJ-42
    python jira.py search "project = PROJ AND statusCategory != Done"
    python jira.py create --type Story --summary "..." --description-file body.md
    python jira.py edit PROJ-42 --summary "..." --description-file body.md
    python jira.py transition PROJ-42 Done
    python jira.py comment PROJ-42 --body-file note.md
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_FILE = Path.home() / ".config" / "jira-cli" / "jira.env"

# Every key the config file and the environment may carry. Only the first three
# are required; JIRA_PROJECT just saves typing --project on every create.
SETTINGS = ("JIRA_SITE", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT")
REQUIRED = SETTINGS[:3]

# v2, for the ADF reason in the module docstring.
API = "/rest/api/2"


def _stdout_utf8() -> None:
    """Windows consoles default to a codepage that cannot print an em dash.

    Every ticket body in this project is full of them, so without this the
    script dies on output rather than on anything that matters.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - very old Pythons
        pass


def load_config() -> dict:
    """Environment first, then the file — so a one-off override needs no edit.

    The file lives outside any repo on purpose. There is no gitignore entry to
    forget, and no way for the token to reach a commit by accident. `JIRA_CONFIG`
    points somewhere else entirely, which is what a CI job or a second account
    wants.
    """
    path = Path(os.environ["JIRA_CONFIG"]) if os.environ.get("JIRA_CONFIG") else CONFIG_FILE

    config = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            config[key.strip()] = value.strip()

    for key in SETTINGS:
        if os.environ.get(key):
            config[key] = os.environ[key]

    missing = [k for k in REQUIRED if not config.get(k)]
    if missing:
        raise SystemExit(
            f"Missing {', '.join(missing)}.\n"
            f"Set them in the environment or in {path}.\n"
            "Create a token at id.atlassian.com/manage-profile/security/api-tokens"
        )
    return config


class JiraError(Exception):
    """An error the API reported, rendered as a sentence rather than a dump.

    Jira returns failures as `errorMessages` (a list) and `errors` (a dict of
    field to message). Printing the raw JSON is how a one-line problem becomes
    unreadable, so both are flattened here — the same call the frontend's API
    client makes for FastAPI's validation errors.
    """


def request(config: dict, method: str, path: str, body=None, params=None):
    url = f"https://{config['JIRA_SITE']}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    token = base64.b64encode(
        f"{config['JIRA_EMAIL']}:{config['JIRA_API_TOKEN']}".encode()
    ).decode()

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req) as response:
            raw = response.read()
            # 204 on edit and transition; there is nothing to decode.
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise JiraError(f"HTTP {exc.code}: {raw[:400]}") from exc

        parts = list(payload.get("errorMessages") or [])
        parts += [f"{field}: {msg}" for field, msg in (payload.get("errors") or {}).items()]
        raise JiraError(f"HTTP {exc.code}: " + ("; ".join(parts) or raw[:400])) from exc
    except urllib.error.URLError as exc:
        raise JiraError(f"Could not reach {config['JIRA_SITE']}: {exc.reason}") from exc


def read_text(source: str) -> str:
    """Long text comes from a file or stdin, never from an argument.

    A ticket body here runs to thousands of characters with tables, code blocks
    and newlines. Passing that as an argv string means shell quoting decides
    what survives, which is both miserable to write and silently lossy.
    """
    if source == "-":
        return sys.stdin.read()
    return Path(source).read_text(encoding="utf-8")


def issue_url(config: dict, key: str) -> str:
    return f"https://{config['JIRA_SITE']}/browse/{key}"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_get(config, args):
    issue = request(config, "GET", f"{API}/issue/{args.key}")
    if args.json:
        print(json.dumps(issue, indent=2))
        return

    fields = issue["fields"]
    print(f"{issue['key']}  {fields['summary']}")
    print(f"  type={fields['issuetype']['name']}  status={fields['status']['name']}")
    print(f"  {issue_url(config, issue['key'])}")
    if fields.get("description"):
        print()
        print(fields["description"])


def cmd_search(config, args):
    """`/search/jql`, because the original `/search` is gone.

    Measured against this site rather than inferred: `GET /rest/api/2/search`
    answers **HTTP 410 Gone** — "The requested API has been removed" — while
    `/search/jql` returns normally. A fallback to the old path was written
    first and deleted once that was known; a branch that can never be taken
    reads as an unresolved doubt rather than as caution.

    The enhanced endpoint also changed the response shape. It returns `isLast`
    and no `total`, which is why the line below counts what came back instead
    of reporting a total it was never given.
    """
    payload = request(
        config, "GET", f"{API}/search/jql",
        params={
            "jql": args.jql,
            "maxResults": args.limit,
            "fields": "summary,status,issuetype",
        },
    )

    issues = payload.get("issues", [])
    if args.json:
        print(json.dumps(payload, indent=2))
        return

    for issue in issues:
        f = issue["fields"]
        print(f"{issue['key']:<10} {f['status']['name']:<14} {f['issuetype']['name']:<8} {f['summary']}")
    print(f"({len(issues)} shown)")


def cmd_create(config, args):
    # --project wins, then JIRA_PROJECT. Neither being set is the mistake a new
    # user is most likely to make, so it gets a sentence rather than a traceback.
    project = args.project or config.get("JIRA_PROJECT")
    if not project:
        raise SystemExit(
            "No project key: pass --project, or set JIRA_PROJECT in the "
            f"environment or in {CONFIG_FILE}."
        )

    fields = {
        "project": {"key": project},
        "issuetype": {"name": args.type},
        "summary": args.summary,
    }
    if args.description_file:
        fields["description"] = read_text(args.description_file)

    created = request(config, "POST", f"{API}/issue", body={"fields": fields})
    print(f"{created['key']} created  {issue_url(config, created['key'])}")


def cmd_edit(config, args):
    fields = {}
    if args.summary:
        fields["summary"] = args.summary
    if args.description_file:
        fields["description"] = read_text(args.description_file)
    if not fields:
        raise SystemExit("Nothing to change: pass --summary and/or --description-file.")

    request(config, "PUT", f"{API}/issue/{args.key}", body={"fields": fields})
    print(f"{args.key} updated ({', '.join(sorted(fields))})")


def cmd_transition(config, args):
    """Resolve the status name to its id here rather than making the caller.

    The MCP server exposed this as two tools and every transition cost two
    round trips — fetch the ids, then post one. The id is an implementation
    detail of the workflow; the name is what a person means.
    """
    available = request(config, "GET", f"{API}/issue/{args.key}/transitions")["transitions"]
    match = next(
        (t for t in available if t["name"].lower() == args.status.lower()
         or t["to"]["name"].lower() == args.status.lower()),
        None,
    )
    if match is None:
        names = ", ".join(sorted(t["name"] for t in available))
        raise SystemExit(f"No transition to {args.status!r} from here. Available: {names}")

    request(config, "POST", f"{API}/issue/{args.key}/transitions",
            body={"transition": {"id": match["id"]}})
    print(f"{args.key} -> {match['to']['name']}")


def cmd_comment(config, args):
    request(config, "POST", f"{API}/issue/{args.key}/comment",
            body={"body": read_text(args.body_file)})
    print(f"{args.key} commented  {issue_url(config, args.key)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jira.py", description="Read and write the Jira board over the REST API."
    )
    parser.add_argument("--json", action="store_true", help="print the raw API response")
    sub = parser.add_subparsers(dest="command", required=True)

    get = sub.add_parser("get", help="show one issue")
    get.add_argument("key")
    get.set_defaults(func=cmd_get)

    search = sub.add_parser("search", help="run a JQL query")
    search.add_argument("jql")
    search.add_argument("--limit", type=int, default=50)
    search.set_defaults(func=cmd_search)

    create = sub.add_parser("create", help="create an issue")
    create.add_argument("--project", help="project key; defaults to JIRA_PROJECT")
    create.add_argument("--type", default="Story")
    create.add_argument("--summary", required=True)
    create.add_argument("--description-file", help="path, or - for stdin")
    create.set_defaults(func=cmd_create)

    edit = sub.add_parser("edit", help="change an issue's summary or description")
    edit.add_argument("key")
    edit.add_argument("--summary")
    edit.add_argument("--description-file", help="path, or - for stdin")
    edit.set_defaults(func=cmd_edit)

    transition = sub.add_parser("transition", help="move an issue to a status")
    transition.add_argument("key")
    transition.add_argument("status")
    transition.set_defaults(func=cmd_transition)

    comment = sub.add_parser("comment", help="add a comment")
    comment.add_argument("key")
    comment.add_argument("--body-file", required=True, help="path, or - for stdin")
    comment.set_defaults(func=cmd_comment)

    return parser


def main(argv=None) -> int:
    _stdout_utf8()
    args = build_parser().parse_args(argv)
    try:
        args.func(load_config(), args)
    except JiraError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
