# cf-automerge

Turn conda-forge bot automerge on or off across every feedstock you maintain, in one go.

## What it does

1. Finds your feedstocks through your conda-forge GitHub team memberships (each feedstock team is synced from `recipe-maintainers`). Archived repos and ones you can't push to are skipped.
2. Reads `conda-forge.yml` in each and prints a summary: current automerge setting per feedstock and how many will change.
3. Asks for confirmation, then commits the change **directly to the default branch** with `[ci skip] ***NO_CI***`, so no builds or uploads are triggered.

No PRs are opened and nothing is cloned. Comments and other keys in `conda-forge.yml` are preserved.

After it runs, new bot PRs (version bumps and migrations) merge automatically once CI is green. Bot PRs that were already open aren't affected; add the `automerge` label to those by hand if you want them merged.

## Setup

You need `gh` (GitHub CLI), Python 3, and `ruamel.yaml`.

**conda (recommended)**

```bash
conda env create -f environment.yml
conda activate cf-automerge
```

**pip / Homebrew (macOS)**

```bash
brew install gh
python3 -m pip install --user -r requirements.txt
```

**Auth** (one time):

```bash
gh auth login              # GitHub.com, HTTPS, browser login
gh auth refresh -s read:org
```

`read:org` is needed to list your conda-forge teams.

> macOS has no `python` command by default, so use `python3`.

## Usage

Always dry-run first:

```bash
python3 cf_enable_automerge.py --dry-run
```

Then for real:

```bash
python3 cf_enable_automerge.py
```

| Option | What it does |
|---|---|
| `--mode true` | Automerge all bot PRs (default) |
| `--mode version` | Automerge only version-bump PRs |
| `--mode migration` | Automerge only migration PRs |
| `--mode off` | Remove automerge from `conda-forge.yml` |
| `--dry-run` | Show summary and diffs, push nothing |
| `--only foo-feedstock bar-feedstock` | Limit to specific feedstocks |
| `-y` | Skip the confirmation prompt |
| `-j N` | Parallel scans (default 8) |

Example output:

```
Scanning 12 feedstocks...

  foo-feedstock          off -> true
  bar-feedstock          true  (no change)

You maintain 12 feedstocks.
  Current automerge: off=9, true=3
  Will change to 'true': 9
  Already 'true':      3

Push to 9 feedstocks? [y/N]
```

## Notes

- Automerge relies on the `automerge.yml` workflow in the feedstock. Any feedstock rerendered in the last few years has it; older ones get it on their next rerender.
- The script edits only `conda-forge.yml` (`bot.automerge`). It's safe to rerun: feedstocks already at the target setting are skipped.
- To undo, run with `--mode off`.
