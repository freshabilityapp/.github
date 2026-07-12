# Author Filtering in GitHub Projects — Implementation Runbook

## Why this exists

GitHub Projects (v2) exposes `status`, `labels`, `assignees`, and custom fields as
filterable properties — but **not** the issue's author. This is a long-standing gap
and a regression from classic Projects:

- [community#9424 — Editable 'author' or 'created by' field](https://github.com/orgs/community/discussions/9424)
- [community#180965 — Filter issues by owner in Projects](https://github.com/orgs/community/discussions/180965)

**The workaround:** add a custom single-select field named `Created By` and
populate it from the issue's author. (`Author` would be the obvious name, but
GitHub reserves it — see Step 1.) Custom fields *are* filterable, so this restores
`created-by:kenwalters` style filtering, grouping, and slicing in project views.

**Prior art check:** no off-the-shelf action does this. `actions/add-to-project`
(official) adds items but cannot set field values
([issue #237](https://github.com/actions/add-to-project/issues/237)).
`nipe0324/update-project-v2-item-field` and `titoportas/update-project-fields` are
the closest third-party options, but both are lightly maintained and neither can
read the issue author during a bulk update. The logic below is ~40 lines of GraphQL,
so we own it rather than take the dependency.

---

## Architecture

Three pieces, all hitting the same GraphQL mutation (`updateProjectV2ItemFieldValue`):

| | Covers | Mechanism |
|---|---|---|
| **Reusable workflow** | New issues, going forward | `issues: [opened, transferred, reopened]` → set field |
| **Python script** | Every issue already on the board | Paginate items → set field |
| **Scheduled backfill** | Drift: new members, missed events | Weekly cron runs the script with `--create-missing-options --force` |

The event workflow lives once in `freshabilityapp/.github` and is called by an
8-line caller in each repo. Fix the logic once; every repo inherits it.

The script does the one-time backfill (Step 3), then keeps running as the weekly
cron. Together with the `External` fallback in the callers, this makes the system
self-healing: an author with no matching option gets bucketed into `External` by
the event workflow, and the next cron run creates their option and reassigns them
(`--force` re-derives every item's Created By from ground truth — safe, because
the issue author never changes). The same run repairs anything the event workflow
missed (missed webhook, Actions outage, workflow disabled).

### Files

All four live in [`freshabilityapp/.github`](https://github.com/freshabilityapp/.github),
the org's shared repo (public):

| File (in this repo) | Role |
|---|---|
| `.github/workflows/set-issue-author.yml` | Reusable event workflow — called by every repo |
| `.github/workflows/backfill-project-author.yml` | Weekly cron + `workflow_dispatch` backfill |
| `scripts/project-author.yml` | **Template** — copy to each repo as `.github/workflows/project-author.yml` (Step 5). Lives outside `.github/workflows/` so it doesn't run here |
| `scripts/backfill_project_author.py` | Backfill script (run by the cron; also runnable locally) |

---

## Prerequisites

- Admin on the `freshabilityapp` org (to create the org secret and the PAT).
- Write access to the project board.
- Push access to [`freshabilityapp/.github`](https://github.com/freshabilityapp/.github),
  the org's shared repo (already created, public).
- Python 3.9+ for the backfill (Step 3 creates a `.venv` and installs `requests`).
- The project number, from its URL: `https://github.com/orgs/freshabilityapp/projects/<N>`.

---

## Step 1 — Create the `Created By` field

In the project → **Settings** → **+ New field**:

- **Name:** `Created By` (exact — the workflow and script look it up by name)
- **Type:** Single select

> **Why not "Author"?** GitHub rejects it: *"This field name is a reserved word"*
> (presumably held back for a future native field — see the discussions linked at
> the top). `Created By` is the fallback name used throughout this setup. If you
> pick something else, it must match `field-name` in every caller workflow and
> `--field` in the backfill cron.

Add one option per team member, named with their **GitHub login** (not their display
name — the workflow matches on `github.event.issue.user.login`).

Add a catch-all option named **`External`** — the caller workflows use it as the
`fallback-option` for any author with no matching option (contributors, bots,
new hires).

> **The single-select tradeoff:** it's a closed vocabulary. A new hire opens an
> issue and no option matches their login. The `External` fallback plus the weekly
> backfill cron (Step 4) close this gap: the item lands in `External`, and the
> next cron run creates the missing option and reassigns it. The only cost is up
> to a week in the `External` bucket — add the option manually if you can't wait.

Apart from `External`, you can skip seeding options entirely and let Step 3's
`--create-missing-options` generate them from the issues already on the board.


## Step 2 — Create the token and org secret

`GITHUB_TOKEN` **cannot** write to org-owned Projects. You need a PAT.

1. Create a **fine-grained PAT** (Settings → Developer settings → Personal access
   tokens → Fine-grained):
   - **Resource owner:** `freshabilityapp`
   - **Repository access:** **All repositories** — covers current *and future*
     repos, so a new repo joining the board never breaks the workflows or the
     weekly cron. (Tighter alternative: *Only select repositories* with every
     repo that feeds the project — but then every new repo needs a token edit,
     and a missed one fails the backfill with
     `Resource not accessible by personal access token`.)
   - **Organization permissions:** Projects → **Read and write**
   - **Repository permissions:** Issues → **Read-only**, and Pull requests →
     **Read-only** (Metadata → Read-only is added automatically). The PR
     permission matters even though only issues get the field: if the board
     contains *any* PR items, scanning their content without it fails with
     `Resource not accessible by personal access token`.
   - Set an expiry you'll actually remember to rotate.

2. Add it as an **organization secret** named `PROJECTS_CREATED_BY_PAT`
   (Org → Settings → Secrets and variables → Actions → New organization secret).
   Grant it to the repos whose issues land on the project, **plus the
   [`freshabilityapp/.github`](https://github.com/freshabilityapp/.github) repo
   itself** — the scheduled backfill (Step 4) runs there and reads this secret.

An org secret means you add the token once, not once per repo — and rotation is a
single edit.

![Dialog modal for adding new PAT](/images/new-personal-access-token-dialog-modal.png)


## Step 3 — Backfill existing issues

Dry run first. Nothing is written without `--apply`.

Set up a virtual environment so `requests` doesn't land in your system Python
(`.venv` is already in `.gitignore`):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests

export GITHUB_TOKEN=github_pat_...   # the PAT from Step 2

python backfill_project_author.py --owner freshabilityapp --project N
```

The remaining commands in this step assume the `.venv` is still active. If you
come back in a new shell later, just re-run `source .venv/bin/activate`.

Read the summary. It lists every author login on the board that has **no matching
option** — these are the issues that would be silently skipped. Two ways to resolve:

- Add the options by hand in the project UI, then re-run, **or**
- Let the script create them:

```bash
python backfill_project_author.py --owner freshabilityapp --project N \
  --create-missing-options --apply
```

> ⚠️ `--create-missing-options` uses the `updateProjectV2Field` mutation, which
> **replaces the entire option list**. The script always resends the existing
> options alongside the new ones, and GitHub preserves option IDs for unchanged
> names — but this is the only operation here that could damage the field. If that
> makes you nervous, test it against a scratch project first, or just add the
> options manually.

Then the real run:

```bash
python backfill_project_author.py --owner freshabilityapp --project N --apply
```

Useful flags:

| Flag | Effect |
|---|---|
| `--apply` | Actually write (default is dry run) |
| `--force` | Overwrite items that already have a Created By value |
| `--field NAME` | Use a field name other than `Created By` |
| `--create-missing-options` | Auto-add options for unmapped logins (see warning above) |

**Verify:** open the project, group by `Created By`. Every issue should be
accounted for.

This is the only manual run you should ever need — after Step 4, the scheduled
workflow repeats it weekly (with `--create-missing-options --force`).

---

## Step 4 — Deploy the reusable workflow

The org's shared repo — [`freshabilityapp/.github`](https://github.com/freshabilityapp/.github)
— exists and is **public**. This is GitHub's convention for org-wide defaults: shared
workflows, issue templates, `CONTRIBUTING.md`, the org profile README.

The files are already laid out in this repo (see the Files table) — deploying is
committing and pushing them:

```bash
git add .github/workflows scripts
git commit -m "Add Created By field workflows: event-driven set + weekly backfill"
git push origin main
```

Yes, the path doubles up — the *repo* is named `.github`, and workflows still live in
a `.github/workflows/` *directory* inside it. That's why callers reference
`freshabilityapp/.github/.github/workflows/set-issue-author.yml@main`.

Before pushing, confirm the real project number in both
`.github/workflows/backfill-project-author.yml` (the `--project` flag) and the
`scripts/project-author.yml` template (`project-number`). The cron fires Mondays 06:17 UTC; adjust the schedule
if weekly is too slow a ceiling for new-hire reassignment. You can also trigger
it any time from the Actions tab (`workflow_dispatch`).

> The cron runs with `--force`, which rewrites the Created By on **every** item each
> week — intentional, so items parked in `External` get reassigned once their
> option exists, and any drift is repaired. The author of an issue never changes,
> so this can only converge. Cost: one mutation per board item per run — fine for
> boards of hundreds, revisit if it grows past that.

Because the repo is **public**, no extra configuration is needed — any repo in the org
can call the workflow. (Had it been private, you'd have to set
Settings → Actions → General → Access to *"Accessible from repositories in the
freshabilityapp organization"*, or every caller would fail with a misleading
"workflow not found".)

Publishing this file publicly is safe: it contains no credentials, only a *reference*
to `secrets.PROJECTS_CREATED_BY_PAT`, which is resolved at run time from the calling repo.

No caller exists yet, so the event workflow won't fire. The cron *will* start
running on schedule — that's fine, it's the same idempotent backfill you already
ran by hand in Step 3.

---

## Step 5 — Wire up each repo

In each repo whose issues land on the project, copy the template
`scripts/project-author.yml` to `.github/workflows/project-author.yml`:

```yaml
name: Project Created By field

on:
  issues:
    types: [opened, transferred, reopened]

jobs:
  set-author:
    uses: freshabilityapp/.github/.github/workflows/set-issue-author.yml@main
    with:
      project-owner: freshabilityapp
      project-number: 18          # <-- your project number
      field-name: Created By      # "Author" is a reserved field name in Projects
      fallback-option: External   # catch-all; the weekly cron reassigns later
    secrets:
      projects-token: ${{ secrets.PROJECTS_CREATED_BY_PAT }}
```

Roll out to **one repo first**. Open a throwaway issue, confirm the Created By
field populates, then propagate.

**Why `addProjectV2ItemById` and not a lookup:** on `issues: opened` there's a race
between our workflow and the project's built-in auto-add. Reading `projectItems` can
come back empty. `addProjectV2ItemById` is idempotent — it returns the existing item
if one is already there — so it sidesteps the race entirely.

---

## Verification

- [ ] Open a test issue in a wired-up repo → Created By populates within ~30s
- [ ] Project view → filter `created-by:<your login>` returns it
- [ ] Group by `Created By` → no unexpected "No Created By" bucket
- [ ] Open an issue as someone with no matching option → workflow **succeeds**,
      item gets `External` (notice in the run log, not a failure)
- [ ] Trigger `Backfill project Created By field` from the Actions tab
      (`workflow_dispatch`) → run succeeds; the `External` item from the previous
      check is reassigned to the real login (option auto-created)

---

## Rollback

Nothing here is destructive to issues — the field lives on the project item only.

1. Delete the caller workflow from each repo (stops new writes).
2. Delete `backfill-project-author.yml` from `freshabilityapp/.github`
   (stops the weekly cron).
3. Delete the `Created By` field in the project settings (removes all values at once).
4. Revoke the PAT and delete the `PROJECTS_CREATED_BY_PAT` org secret.

---

## Maintenance

| Situation | Action |
|---|---|
| New team member | Nothing — their issues land in `External`, and the next weekly cron creates their option and reassigns them. Add the option manually only if a week is too long to wait |
| Issues missing/wrong Created By (missed webhook, outage) | Nothing — the weekly cron repairs them (`--force`). To fix immediately, trigger `Backfill project Created By field` from the Actions tab |
| New repo added to the project | Copy in the caller workflow; grant it the org secret |
| PAT expiry | Rotate the token, update the org secret — single edit. If the cron starts failing, this is the first thing to check |

The scheduled backfill is the safety net for the event-driven half — if it's ever
noisy or misbehaving, disable the workflow in the Actions tab rather than deleting
it, and fall back to manual `workflow_dispatch` runs.

## Watch for

GitHub could ship a native author filter and make all of this redundant. Both
discussions linked at the top are still open — worth a glance before you sink time
into extending this.
