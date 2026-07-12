# Author Filtering in GitHub Projects — Implementation Runbook

## Why this exists

GitHub Projects (v2) exposes `status`, `labels`, `assignees`, and custom fields as
filterable properties — but **not** the issue's author. This is a long-standing gap
and a regression from classic Projects:

- [community#9424 — Editable 'author' or 'created by' field](https://github.com/orgs/community/discussions/9424)
- [community#180965 — Filter issues by owner in Projects](https://github.com/orgs/community/discussions/180965)

**The workaround:** add a custom single-select field named `Author` and populate it
from the issue's author. Custom fields *are* filterable, so this restores
`Author: ken` style filtering, grouping, and slicing in project views.

**Prior art check:** no off-the-shelf action does this. `actions/add-to-project`
(official) adds items but cannot set field values
([issue #237](https://github.com/actions/add-to-project/issues/237)).
`nipe0324/update-project-v2-item-field` and `titoportas/update-project-fields` are
the closest third-party options, but both are lightly maintained and neither can
read the issue author during a bulk update. The logic below is ~40 lines of GraphQL,
so we own it rather than take the dependency.

---

## Architecture

Two halves, both hitting the same GraphQL mutation (`updateProjectV2ItemFieldValue`):

| | Covers | Mechanism |
|---|---|---|
| **Reusable workflow** | New issues, going forward | `issues: [opened, transferred, reopened]` → set field |
| **Python script** | Every issue already on the board | Paginate items → set field |

The workflow lives once in `freshabilityapp/.github` and is called by an 8-line
caller in each repo. Fix the logic once; every repo inherits it.

The script is **idempotent** — it skips items that already have a value. It's the
one-time backfill, but it's also the repair tool if the event workflow ever misses
an issue (missed webhook, Actions outage, workflow disabled). Keep it around.

### Files

| File | Destination |
|---|---|
| `set-issue-author.yml` | [`freshabilityapp/.github`](https://github.com/freshabilityapp/.github) (public, org shared repo) → `.github/workflows/set-issue-author.yml` |
| `call-set-issue-author.yml` | Each repo → `.github/workflows/project-author.yml` |
| `backfill_project_author.py` | Run locally (or as a `workflow_dispatch` job) |

---

## Prerequisites

- Admin on the `freshabilityapp` org (to create the org secret and the PAT).
- Write access to the project board.
- Push access to [`freshabilityapp/.github`](https://github.com/freshabilityapp/.github),
  the org's shared repo (already created, public).
- Python 3.9+ with `requests` for the backfill.
- The project number, from its URL: `https://github.com/orgs/freshabilityapp/projects/<N>`.

---

## Step 1 — Create the `Author` field

In the project → **Settings** → **+ New field**:

- **Name:** `Author` (exact — the workflow and script look it up by name)
- **Type:** Single select

Add one option per team member, named with their **GitHub login** (not their display
name — the workflow matches on `github.event.issue.user.login`).

Optionally add a catch-all option like `External` for contributors and bots.

> **The single-select tradeoff:** it's a closed vocabulary. A new hire opens an
> issue, no option matches their login, and the item silently gets no Author. Both
> the workflow and the script surface this as a warning rather than failing — but
> you have to act on it. If your contributor set is open-ended, a Text field would
> be more robust at the cost of dropdown/grouping UX.

You can skip seeding options entirely and let Step 3's `--create-missing-options`
generate them from the issues already on the board.

---

## Step 2 — Create the token and org secret

`GITHUB_TOKEN` **cannot** write to org-owned Projects. You need a PAT.

1. Create a **fine-grained PAT** (Settings → Developer settings → Personal access
   tokens → Fine-grained):
   - **Resource owner:** `freshabilityapp`
   - **Organization permissions:** Projects → **Read and write**
   - **Repository permissions:** Issues → **Read-only** (select all repos that feed
     the project)
   - Set an expiry you'll actually remember to rotate.

2. Add it as an **organization secret** named `PROJECTS_PAT`
   (Org → Settings → Secrets and variables → Actions → New organization secret).
   Grant it to the repos whose issues land on the project.

An org secret means you add the token once, not once per repo — and rotation is a
single edit.

---

## Step 3 — Backfill existing issues

Dry run first. Nothing is written without `--apply`.

```bash
pip install requests
export GITHUB_TOKEN=github_pat_...   # the PAT from Step 2

python backfill_project_author.py --owner freshabilityapp --project N
```

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
| `--force` | Overwrite items that already have an Author value |
| `--field NAME` | Use a field name other than `Author` |
| `--create-missing-options` | Auto-add options for unmapped logins (see warning above) |

**Verify:** open the project, group by `Author`. Every issue should be accounted for.

---

## Step 4 — Deploy the reusable workflow

The org's shared repo — [`freshabilityapp/.github`](https://github.com/freshabilityapp/.github)
— exists and is **public**. This is GitHub's convention for org-wide defaults: shared
workflows, issue templates, `CONTRIBUTING.md`, the org profile README.

Commit `set-issue-author.yml` there at:

```
.github/workflows/set-issue-author.yml
```

Yes, the path doubles up — the *repo* is named `.github`, and workflows still live in
a `.github/workflows/` *directory* inside it. That's why callers reference
`freshabilityapp/.github/.github/workflows/set-issue-author.yml@main`.

```bash
git clone https://github.com/freshabilityapp/.github.git freshability-dotgithub
cd freshability-dotgithub
mkdir -p .github/workflows
cp /path/to/set-issue-author.yml .github/workflows/
git add .github/workflows/set-issue-author.yml
git commit -m "Add reusable workflow: set Author field on project item"
git push origin main
```

Because the repo is **public**, no extra configuration is needed — any repo in the org
can call the workflow. (Had it been private, you'd have to set
Settings → Actions → General → Access to *"Accessible from repositories in the
freshabilityapp organization"*, or every caller would fail with a misleading
"workflow not found".)

Publishing this file publicly is safe: it contains no credentials, only a *reference*
to `secrets.PROJECTS_PAT`, which is resolved at run time from the calling repo.

No caller exists yet, so nothing will run. This step just publishes the workflow.

---

## Step 5 — Wire up each repo

In each repo whose issues land on the project, add `.github/workflows/project-author.yml`:

```yaml
name: Project author field

on:
  issues:
    types: [opened, transferred, reopened]

jobs:
  set-author:
    uses: freshabilityapp/.github/.github/workflows/set-issue-author.yml@main
    with:
      project-owner: freshabilityapp
      project-number: 1           # <-- your project number
      field-name: Author
      # fallback-option: External # optional catch-all for unmapped logins
    secrets:
      projects-token: ${{ secrets.PROJECTS_PAT }}
```

Roll out to **one repo first**. Open a throwaway issue, confirm the Author field
populates, then propagate.

**Why `addProjectV2ItemById` and not a lookup:** on `issues: opened` there's a race
between our workflow and the project's built-in auto-add. Reading `projectItems` can
come back empty. `addProjectV2ItemById` is idempotent — it returns the existing item
if one is already there — so it sidesteps the race entirely.

---

## Verification

- [ ] Open a test issue in a wired-up repo → Author populates within ~30s
- [ ] Project view → filter `Author: <your login>` returns it
- [ ] Group by `Author` → no unexpected "No Author" bucket
- [ ] Open an issue as someone with no matching option → workflow **succeeds** with
      a warning in the run log (does not fail the run)

---

## Rollback

Nothing here is destructive to issues — the field lives on the project item only.

1. Delete the caller workflow from each repo (stops new writes).
2. Delete the `Author` field in the project settings (removes all values at once).
3. Revoke the PAT and delete the `PROJECTS_PAT` org secret.

---

## Maintenance

| Situation | Action |
|---|---|
| New team member | Add their login as an option in the project field |
| Issues missing an Author (missed webhook, outage) | Re-run the backfill script — it only touches empty items |
| New repo added to the project | Copy in the caller workflow; grant it the org secret |
| PAT expiry | Rotate the token, update the org secret — single edit |

Consider registering the backfill script as a `workflow_dispatch` job (or a monthly
cron) rather than treating it as a throwaway. It's the safety net for the
event-driven half.

## Watch for

GitHub could ship a native author filter and make all of this redundant. Both
discussions linked at the top are still open — worth a glance before you sink time
into extending this.
