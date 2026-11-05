# kobo-espo-bridge

Kobo → EspoCRM, and back. A small service that polls KoboToolbox for new submissions and
turns each one into an EspoCRM record (a case, a contact, a site...), routed to the right
team, with the right people notified. It also keeps lists from EspoCRM (open cases, sites,
standards...) on Kobo forms as CSV media, so forms can offer them as choices. And it applies
follow-up visits and edited submissions to the records they're about. Stdlib Python only;
one process per template.

Extracted from [kobo-in-a-box](../kobo-in-a-box) `remote/espocrm/` (W11, 2026-11-03), where
it runs offline on the box, so that the box, [kobo-espo](../kobo-espo) and later connections
share one tested bridge. Formerly kobo-bridge (renamed 2026-11-05). Not to be confused with
kobo-espo, which is one EspoCRM instance using it (its setup, forms, templates and tests).
The bridge's default EspoCRM API user is still called `kobo-bridge`, so existing setups
keep their user.

| File | What it is |
|---|---|
| `bridge.py` | The bridge: `run` (poll every `BRIDGE_POLL_SECONDS`), `once`, `failed DIR` |
| `setup.py` | Sets EspoCRM up for a template: fields, teams, the bridge's API users with only the rights the template needs |
| `fake_kobo.py` | A fake Kobo (the KPI v2 endpoints the bridge uses), for end-to-end tests without a Kobo server |
| `templates/health-referral.toml` | kobo-in-a-box's scenario (community health referrals) |
| `tests/` | Unit tests, Kobo and EspoCRM faked in memory: `PYTHONPATH=.:tests python3 -m unittest discover -s tests` |

## How it works, and why

- **Polling, not Kobo's REST Services.** Nothing changes in Kobo's config, no webhook proxy,
  and after either side has been down the next pass catches up. The cursor is the last
  submission `_id` per project.
- **Never twice.** Before creating a record the bridge looks it up by the submission's
  uuid; with `[record] match`, also by a field (an email, a site code). A lost cursor or
  ledger means a rescan, never a duplicate.
- **One bad submission never blocks the rest.** A submission EspoCRM refuses (a 4xx naming
  the field), or one missing a required answer, is recorded in the ledger
  (`bridge.sqlite`) and retried after 1 min, 10 min, 1 h, then every 6 h, and at once when
  the template or the form changes. "Unreachable" (no connection, 5xx, a refused key) ends
  the pass with the cursor kept. `bridge.py failed DIR` lists what's waiting and why;
  `status.json` has counts and errors, never answers.
- **Least privilege.** Each template's API user can do only what that template needs
  (`setup.py` builds its role): create records of its entity; with `match`, update them;
  with `[requester]`, Contacts and Accounts. It can't read users or delete anything.
- **Only free EspoCRM features**: fields, layouts, roles, the API, and formula. No BPM or
  workflows (the paid Advanced Pack): notifications are @mentions in a note, which core
  EspoCRM turns into notifications (in the app; by email when SMTP is set up).

## Templates

A template (TOML) says which Kobo form becomes which records, and how. All sections but
`[record]` are optional.

```toml
[bridge]                      # the API user setup.py makes for this template
user = "kobo-intake"          # default kobo-bridge
role = "Kobo intake"          # default "Kobo bridge"
secret = "ESPO_INTAKE_API_KEY"  # its key's name in the secrets file

[form]
name = "Kobo support request" # every deployed project with this name the token can see
when = { needs_followup = "yes" }  # only these submissions become records

[record]                      # ([case] is its older name)
entity = "Case"               # any entity: Contact, CSite...
name = "{category}: {summary}"  # {question}: the answer's label
details = [["Notes", "notes"], ["", "description"]]  # lines of the description
required = ["email", "summary"]  # checked before anything is written
match = { emailAddress = "email" }  # update the record that has this answer, don't add one
single_team = true            # one team: the highest-priority matching route's
set = { cSource = "Kobo form" }  # constants
media = "attachments"         # copy the submission's files into this attachment field
media_max_mb = 10             # bigger files are named in the description instead
attachments = true            # (or) links to the files in Kobo; they need a Kobo login
panel = "Request"             # the record page's panel with the template's fields
notify_message = "New request from Kobo:"

[requester]                   # link the record to who sent it
email = "requester_email"     # Contact by email, made if missing
name = "requester_name"
organisation = "organisation" # Account by the answer's label, made if missing
create_account = true

[escalation]                  # unassigned this long after it came in: mention the route's `escalate` users
after_hours = 4

[[route]]                     # every matching route adds its team (or, single_team, the top one)
match = { project = "consortium_a" }  # question = answer; for select_multiple, "includes"
team = "Project: Consortium A"
priority = "High"             # cases
follow_up_hours = 48          # cases: the "Follow up by" date
notify = ["triager"]          # mentioned on each new record
escalate = ["lead-a"]

[[field]]                     # Kobo question -> EspoCRM field
kobo = "category"
espo = "cCategory"            # custom fields of EspoCRM's own entities start with "c"
type = "enum"                 # varchar | text | int | date | datetime | enum | multiEnum | personName | link;
                              # left out for select questions (the form says) or existing fields
label = "Category"
options = { forms = "Forms and XLSForm" }  # left out: from the deployed form

[[field]]                     # a link: the record whose `lookup` field has the answer
kobo = "standard"
espo = "standard"             # the link (made in EspoCRM, not by setup.py)
type = "link"
entity = "CStandard"          # setup.py lets the bridge's user read it
lookup = "code"               # left out: the answer is the record's id (from a [[list]] of ids)

[[field]]
kobo = "end"                  # start, end, datetime questions
espo = "registeredAt"
type = "datetime"             # stored in UTC, from Kobo's local time and offset

[[list]]                      # EspoCRM records as a CSV on Kobo forms (select_one_from_file, pulldata)
file = "sites.csv"
entity = "CSite"
team = "Response: Test EQ 1"  # only this team's records
where = { status = ["operational", "needs_repair"] }
columns = { name = "siteCode", label = "{siteCode} · {name}", status = "status" }
forms = ["Site visit"]

[followup]                    # a second form that updates existing records
form = "Site visit"
key = "site"                  # the question that picks the record
match = "siteCode"            # ...by this field (left out: cases by number, cs_<n>)
fields = { condition = "status", visit_date = "lastVisit" }  # only these are written
note = [["By", "visitor"], ["Notes", "visit_notes"]]
user = "kobo-site-visits"     # its own API user: may edit only those fields
media = true                  # the visit's photos, on its note
```

**Media.** With `media`, the files a submission's answers refer to (in repeat groups too;
not deleted ones) are downloaded with the bridge's token, from its own Kobo only (the
URL's host is checked, so a submission can't make it fetch elsewhere), and attached to the
record before it's created. Staff then see them without a Kobo account, with the record's
access rules. As in kobo2logie (`src/lib/kobo.ts`, `attachmentsToForward`). Kobo stores a
file under a cleaned-up name (`site photo (1).jpg` as `site_photo_1.jpg`) and current
servers also give the answer's (`media_file_basename`); for older ones that don't, the
answer is matched by the cleaned-up name. The copy keeps the answer's name.

**Links by lookup.** A `link` field with `lookup` links the one record of `entity` whose
`lookup` field equals the answer: a standard by its code, a district by its pcode. If
there's none, or more than one, the submission is recorded with the count (never a guess)
and tried again on the usual backoff, so a record made later in EspoCRM still gets it.
Looked up before anything is written. (As kobo-connect's `Entity.link.field` headers do,
rodekruis/kobo-connect; its other EspoCRM features the bridge had already.)

**Datetimes.** Kobo sends `2026-04-23T20:21:06.502+05:30` (the phone's local time and
offset); `datetime` fields store it as EspoCRM's UTC `2026-04-23 14:51:06`. Without an
offset it's taken as UTC. Something else is recorded as `not a date and time`.

kobo-in-a-box's template also has `[espo]` (offline, application name), `[[role]]`,
`[[api_user]]` and `[demo]`, for its staff roles, its assistant and demo users.

**Lists reach every phone with the form.** Only the listed columns go into the CSV; keep
names, phone numbers and other personal data out of them.

## Running it

```bash
# Set EspoCRM up for a template (admin password in the secrets file; API keys are written there)
python3 -I setup.py --template my.toml --url https://espo.example.org --secrets /root/espo.secrets \
    --kobo-url https://kf.example.org --token-file /root/kobo.token

# The bridge (in Docker: python:3.13-slim, this directory mounted read-only)
ESPO_URL=https://espo.example.org ESPO_API_KEY=... KOBO_URL=https://kf.example.org \
KOBO_TOKEN_FILE=/root/kobo.token BRIDGE_TEMPLATE=my.toml BRIDGE_STATE_DIR=/var/lib/bridge \
    python3 -I -u bridge.py run    # -u: logs as they happen (-I ignores PYTHONUNBUFFERED)
```

Optional: `ESPO_FOLLOWUP_API_KEY` (follow-ups), `ESPO_EDITS_API_KEY` (edited submissions,
cases only), `BRIDGE_POLL_SECONDS` (30), `BRIDGE_LIST_MIN_SECONDS` (900: each list upload
is a redeploy of the form), `ESPO_LANGUAGE`.

## Tested

- **Unit tests:** 53 (the box's 35, and 18 for records other than cases, requesters,
  matching, one team, mentions, escalation, lists, media and its file names, links by
  lookup and datetimes).
- **End to end** against EspoCRM 10.0.11: kobo-espo's `tests/cases/connection.py`, 73
  checks with the fake Kobo, 72 on kf.kobotoolbox.org (see its REPORT.md).
- **kobo-in-a-box**'s own e2e (60 checks on the rig) ran against the version imported here,
  not yet against this one: run it before the box switches.

## For kobo-in-a-box, when it switches

- Point `espocrm.sh` at this repo's `bridge.py` and `setup.py`. `setup.py` now needs
  `--template` (no default), and reads Kobo from `--kobo-url`/`KOBO_SETUP_URL` and
  `--kobo-host`/`KOBO_SETUP_HOST` (no box defaults).
- Its roles, its assistant and its demo users are now in `templates/health-referral.toml`
  (`[[role]]`, `[[api_user]]`, `[demo]`, `[espo]`), not in the code.
- EspoCRM 10 no longer installs with all of `/var/www/html` on one volume (the box uses
  9.3.11): mount `data`, `custom` and `client/custom` separately
  ([docs](https://docs.espocrm.com/administration/docker/installation/#migration-to-espocrm-10)).
