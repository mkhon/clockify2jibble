# clockify2jibble

Migrates a Clockify workspace to Jibble through both APIs: clients, projects,
tasks, members with their hourly rates, and time entries. It runs in three
steps: export, plan, apply. Every step can be rerun, and `apply` picks up where
it stopped.

Single file, Python 3.7+ standard library only. No dependencies to install.

## Setup

1. Copy `dot.env` to `.env` and fill in the keys:
   - `CLOCKIFY_API_KEY`: Clockify → Profile settings → API. Use a workspace
     admin's or owner's key, so the script can read everyone's entries.
   - `JIBBLE_CLIENT_ID` and `JIBBLE_CLIENT_SECRET`: Jibble → Organization
     settings → API Credentials. Only the owner can create them. The person
     who owns the credentials is the **admin** described in
     [Project visibility](#project-visibility).
   - `CLOCKIFY_WORKSPACE_ID`: only needed if the key sees several workspaces.
     `export` lists them.
   - `CLOCKIFY_BASE_URL`: only for regional Clockify workspaces, e.g.
     `https://euc1.clockify.me/api/v1`.
2. Use the same billing currency in Jibble as in Clockify. If they differ,
   member rates are not set.

## Usage

```bash
python3 migrate.py export   # read Clockify into export.json (read-only)
python3 migrate.py plan     # compare with Jibble, write plan.json and the CSV reports (read-only)
python3 migrate.py apply    # create everything plan.json lists in Jibble
```

Run `plan` and read its summary before `apply`. The summary shows what will be
created, what already exists in Jibble, each member's rate, how many
overlapping entries are moved, and how many entries are skipped and why.
`plan --skip-overlaps` skips overlapping entries instead of moving them; see
[Overlapping entries](#overlapping-entries).

`apply` options for trying it out on a small scale first:

| Option | Effect |
|---|---|
| `--limit N` | import only the next N time entries |
| `--only-person EMAIL` | import only this person's time entries |

Projects and activities are archived only by a full run without these options.

## How Clockify maps to Jibble

| Clockify | Jibble |
|---|---|
| Client | Client, matched by name |
| Project | Project with the same client, matched by name. When two projects share a name, the one with the most entries keeps it and the others become `Name (Client)`. Archived projects are archived once their entries are in. |
| Task | Activity named `Project: Task`. Done tasks are archived once their entries are in. |
| Member | Person, matched by email. New people are created **without** an invitation email. |
| Hourly rate | The person's billable rate. Jibble has one rate per person, so this is the rate on their latest entry with a rate. Jibble applies it to all of their hours, so older billable amounts differ from Clockify's. |
| Time entry | A clock-in with project and activity, plus a clock-out |
| Description | The note on the clock-in, cut to 500 characters. The clock-out has no note. Descriptions of entries that aren't imported are kept in the CSV reports. |

**Not migrated**, because Jibble has no place for them: billable flags, tags,
and workspace, project, task and per-entry rates.

### Overlapping entries

Clockify lets one person's entries overlap, and its reports count overlapping
time twice. Jibble keeps one clock-in/clock-out sequence per person, so two
entries can't cover the same time.

By default `plan` keeps every entry that doesn't overlap an earlier one as it
is, and **moves** each overlapping entry, at its full length, to the first free
slot at or after its original start. Hours per person and project then match
Clockify's reports; the moved entries' clock times don't. An entry that would
only fit after the export time is skipped instead, because people may already
be tracking time in Jibble from then on.

With `plan --skip-overlaps`, overlapping entries are left out instead. To
import one later, change **that** entry in Clockify so it no longer overlaps,
then run `export`, `plan` and `apply` again.

Either way, `overlaps.csv` lists every overlapping entry with its status
(`moved` or `skipped`), why it was skipped, its original times and, if moved,
its new times.

Changes to entries that are already imported are never synced to Jibble.
`plan` places entries using the export alone and doesn't know where entries
already in Jibble actually are, so after a re-export that changes earlier
entries, check `overlaps.csv` before running `apply`.

An entry starting at the exact second the previous one ended is moved 1 second
later, because Jibble rejects a clock-in at the same time as a clock-out.

### Skipped entries

`plan` writes the entries it won't import to two CSV files, with the person,
client, project, task, times, description, billable flag, rate and the reason.
Overlapping entries skipped with `--skip-overlaps` are in `overlaps.csv`
instead.

- **`skipped.csv`:** entries from before 2020-01-01, which Jibble refuses.
- **`errors.csv`:** entries with another problem in the Clockify data:
  - **zero or negative duration**
  - **still running in Clockify**
  - **member not in the workspace's user list**

### Archived projects and activities

Projects archived in Clockify, and activities from tasks marked done, are
archived in Jibble by a full `apply` run once all their entries are in.

Jibble refuses clock-ins on an archived project or activity. When a later run
has entries for one the script created (moved overlaps, or new entries after a
re-export), `apply` reopens it for the run and archives it again when the run
ends, also when the run is interrupted. Projects and activities that existed in
Jibble before the migration are never reopened or archived.

### Project visibility

In Jibble, a project with assignees can be used only by those people. `apply`:

- assigns every project it created to the admin, the owner of the Jibble API
  credentials, so the project is hidden from everyone else;
- adds each person to a project before importing their entries on it, and also
  for entries imported earlier, so everyone who tracked time on a project can
  see it;
- leaves projects that already existed in Jibble as they are, apart from adding
  people who need them.

## Restarting and reruns

`apply` records everything it creates in `state.json` and skips it on the next
run, so it is safe to interrupt and run again:

- Everything not marked done is retried, failures included.
- Jibble objects get IDs derived from the Clockify IDs. Re-sending an object
  that already exists is recognised and counted as done, never duplicated.
- The summary line separates current errors from old ones for items no longer
  in the plan.

To bring over entries tracked in Clockify since the last run, run `export`,
`plan` and `apply` again. Only what is new is created.

Don't run `apply` on two machines at once: each keeps its own `state.json` and
can't see what the other has done.

## Rate limits

- **Clockify:** the free plan allows 30 API requests an hour, so `export` can
  take hours. The trial and paid plans allow 20 a second or more. Responses are cached
  in `.cache/`, so an interrupted export resumes without repeating requests. A
  completed export clears the cache.
- **Jibble:** 20 requests per endpoint per API user in a ~30-second window, and
  180 in total. Every time entry is two requests to the same endpoint, so
  `apply` imports about 20 entries a minute. Running several copies doesn't
  help, because they share the same allowance.

The script waits as long as each API asks (`Retry-After`), fetches a new Jibble
token when the old one expires (after 1 hour), and retries network errors.

## Files

| File | Contents | In git |
|---|---|---|
| `migrate.py` | the script | yes |
| `dot.env` | template for `.env` | yes |
| `jibble-docs/` | Jibble's API collection and OData metadata, for reference | yes |
| `.env` | your keys | no |
| `export.json`, `.cache/` | Clockify data | no |
| `plan.json` | the mapping | no |
| `skipped.csv`, `errors.csv` | the entries left out: too old, or with data problems | no |
| `overlaps.csv` | overlapping entries, moved or skipped, with old and new times | no |
| `state.json` | what `apply` has created, and errors | no |

The untracked files contain your team's names, emails, rates and entry
descriptions. Keep them out of the repository.
