# jira-cli

[![tests](https://github.com/evansnicholasa/jira-cli/actions/workflows/tests.yml/badge.svg)](https://github.com/evansnicholasa/jira-cli/actions/workflows/tests.yml)

A single-file Jira Cloud CLI with **no dependencies** — read and write a board
from any shell, editor, CI job, or AI coding agent.

```bash
python jira.py get PROJ-42
python jira.py search "project = PROJ AND statusCategory != Done"
python jira.py create --type Story --summary "..." --description-file body.md
python jira.py edit PROJ-42 --description-file body.md
python jira.py transition PROJ-42 Done
python jira.py comment PROJ-42 --body-file note.md
```

## Why

I was driving a Jira board through Atlassian's hosted Rovo MCP server. It works
well, but it meters against Rovo credits and **Rovo is not offered on the Free
plan** — so keeping that one integration alive meant paying for a tier I
otherwise had no use for.

The REST API underneath it is available on every plan, including Free, with no
metering. This is about 330 lines of Python talking to it directly.

Two things turned out better rather than merely cheaper:

- **It runs anywhere there is a shell.** The same file serves a terminal, an
  editor, a CI job, and more than one AI coding agent. An MCP server only works
  where it has been configured.
- **It can be read, tested and changed.** A hosted service cannot.

## Install

There isn't one. Download `jira.py` and run it with any Python 3.9+.

No `requests`, no virtualenv, no lockfile. That is a working decision, not an
aesthetic one: a tool you reach for every few weeks is one whose dependencies
rot between uses, and the first run after six months should not begin with a
resolver error.

## Setup

Create an API token at
[id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens),
then write `~/.config/jira-cli/jira.env`:

```
JIRA_SITE=your-site.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=your-token
JIRA_PROJECT=PROJ
```

- `JIRA_PROJECT` is optional — it is the default project key for `create`.
- **The environment overrides the file**, so a one-off run against another site
  needs no edit to the file holding your credential.
- **`JIRA_CONFIG` points the loader somewhere else entirely**, which is what a
  CI job or a second account wants.

The config file lives outside any repo on purpose: there is no `.gitignore`
entry to forget, and no way for the token to reach a commit by accident.

## Commands

| Command | What it does |
|---|---|
| `get KEY` | Show one issue — type, status, URL, description |
| `search JQL` | Run a JQL query |
| `create` | Create an issue (`--type`, `--summary`, `--description-file`) |
| `edit KEY` | Change summary and/or description |
| `transition KEY STATUS` | Move an issue, **by status name** |
| `comment KEY` | Add a comment (`--body-file`) |

`--json` on any command prints the raw API response instead.

**Long text comes from a file or stdin (`-`), never from an argument.** A real
ticket body runs to thousands of characters with tables and code blocks, and
passing that as an argv string lets shell quoting decide what survives.

**`transition` takes a status name, not a transition id.** Jira's API requires
the id, which means fetching the available transitions first; the tool does that
round trip for you, because the id is an implementation detail of the workflow
and the name is what a person means. When the status is not reachable from where
the issue currently sits, the error lists the ones that are — "no" on its own is
a useless answer.

## Design notes

**API v2, not v3.** v3 requires Atlassian Document Format — a JSON tree — for
descriptions and comments, so writing a ticket with headings, tables and code
blocks would mean building a markdown-to-ADF converter first. v2 accepts wiki
markup, which expresses all of it directly (`h2.`, `*bold*`, `{code}`,
`||header||`), and returns readable markup instead of a tree that has to be
walked to be read at all. The cost is that v3 is the version Atlassian calls
current; both have been live for years, and if v2 is withdrawn the converter is
a known, bounded piece of follow-up rather than a surprise.

**There is no `delete` command, deliberately.** Nothing in this workflow deletes
an issue often enough to justify a command that can be run by mistake. The one
throwaway issue created while verifying this was deleted by hand.

**Errors are sentences.** Jira reports failures in two places at once —
`errorMessages` (a list) and `errors` (a field-to-message map) — and printing the
raw JSON is how a one-line problem becomes unreadable. Both are flattened.

**Two things the documentation does not lead with**, both found by probing a live
site rather than by reading:

- `GET /rest/api/2/search` answers **HTTP 410 Gone**. `/search/jql` is the
  replacement. A fallback to the old path was written first and then deleted —
  a branch that can never be taken reads as an unresolved doubt rather than as
  caution.
- That endpoint returns `isLast` and **no `total`**, so the tool counts what came
  back rather than reporting a total it was never given.

## Tests

```bash
python -m unittest discover
```

18 tests, no network, nothing to install — the same property the tool claims for
itself. `unittest` rather than pytest for exactly that reason: the part most
likely to be run by someone checking whether this still works should not begin
with `pip install`.

The tests cover the logic that sits *around* the HTTP call, which is where the
mistakes actually are — config precedence, project-key resolution, the two error
shapes, an empty `204` body, file-or-stdin input, and status-name resolution.
All six commands were exercised against a live board before any of them were
written: an issue was created, read back, searched, edited, commented on and
transitioned, with a description round-tripping byte-identical through a table,
a code block and non-ASCII text — then deleted.

## License

MIT
