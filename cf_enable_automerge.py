#!/usr/bin/env python3
"""Turn bot automerge on/off for every conda-forge feedstock you maintain.

Finds feedstocks via your conda-forge team memberships (each feedstock has a
team synced from recipe-maintainers), scans conda-forge.yml in each, prints a
summary, then (after confirmation) commits the change straight to the default
branch with [ci skip] so no builds fire.

Requires: gh (authed, with read:org scope), ruamel.yaml
Usage:
    python cf_enable_automerge.py --dry-run          # scan + summary + diffs, change nothing
    python cf_enable_automerge.py                    # automerge: true
    python cf_enable_automerge.py --mode version     # only version-bump PRs
    python cf_enable_automerge.py --mode off         # remove automerge everywhere
    python cf_enable_automerge.py --only foo-feedstock bar-feedstock
    python cf_enable_automerge.py -y                 # skip confirmation prompt
"""
import argparse
import base64
import difflib
import json
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from io import StringIO

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

ORG = "conda-forge"
CFG = "conda-forge.yml"
OFF = object()  # sentinel: remove the key


def gh(*args, stdin=None, lines=False):
    r = subprocess.run(["gh", "api", *args], capture_output=True, text=True, input=stdin)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    if lines:
        return [l for l in r.stdout.splitlines() if l.strip()]
    return json.loads(r.stdout) if r.stdout.strip() else None


def my_feedstocks():
    teams = gh("--paginate", "/user/teams",
               "--jq", f'.[] | select(.organization.login=="{ORG}") | .slug', lines=True)
    repos = set()
    for t in teams:
        repos.update(gh("--paginate", f"/orgs/{ORG}/teams/{t}/repos", "--jq",
                        '.[] | select(.name|endswith("-feedstock")) '
                        '| select(.archived|not) | select(.permissions.push) | .name',
                        lines=True))
    return sorted(repos)


def yaml_rt():
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


def show(v):
    return "off" if v in (None, False) else ("true" if v is True else str(v))


def patch(text, mode):
    """Return (current_value, new_text). new_text is None if no change needed."""
    y = yaml_rt()
    data = (y.load(text) if text.strip() else None) or CommentedMap()
    bot = data.get("bot")
    current = bot.get("automerge") if bot else None

    if mode is OFF:
        if current in (None, False):
            return current, None
        del bot["automerge"]
        if not bot:
            del data["bot"]
    else:
        if current == mode:
            return current, None
        if bot is None:
            bot = data["bot"] = CommentedMap()
        bot["automerge"] = mode

    buf = StringIO()
    y.dump(data, buf)
    return current, buf.getvalue()


def scan(repo, mode):
    path = f"/repos/{ORG}/{repo}/contents/{CFG}"
    try:
        cur = gh(path)
        old, sha = base64.b64decode(cur["content"]).decode(), cur["sha"]
    except RuntimeError as e:
        if "404" not in str(e):
            return dict(repo=repo, error=str(e))
        old, sha = "", None
    current, new = patch(old, mode)
    return dict(repo=repo, path=path, old=old, new=new, sha=sha, current=current)


def apply(r, msg):
    body = {"message": msg, "content": base64.b64encode(r["new"].encode()).decode()}
    if r["sha"]:
        body["sha"] = r["sha"]
    gh("-X", "PUT", r["path"], "--input", "-", stdin=json.dumps(body))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["true", "version", "migration", "off"], default="true",
                   help="target bot.automerge (default: true = all bot PRs; off = remove it)")
    p.add_argument("--dry-run", action="store_true", help="show diffs, change nothing")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before pushing")
    p.add_argument("--only", nargs="+", metavar="FEEDSTOCK", help="limit to these repos")
    p.add_argument("-j", "--jobs", type=int, default=8, help="parallel scans (default 8)")
    a = p.parse_args()
    mode = {"true": True, "off": OFF}.get(a.mode, a.mode)
    target = "off" if mode is OFF else show(mode)
    msg = (f"Disable bot automerge" if mode is OFF else f"Set bot automerge: {target}") \
        + " [ci skip] ***NO_CI***"

    print("Finding your feedstocks...", flush=True)
    repos = a.only or my_feedstocks()
    print(f"Scanning {len(repos)} feedstocks...\n", flush=True)
    with ThreadPoolExecutor(a.jobs) as ex:
        results = list(ex.map(lambda r: scan(r, mode), repos))

    errors = [r for r in results if "error" in r]
    ok = [r for r in results if "error" not in r]
    todo = [r for r in ok if r["new"] is not None]

    for r in results:
        if "error" in r:
            line = f"ERROR: {r['error']}"
        elif r["new"] is None:
            line = f"{show(r['current'])}  (no change)"
        else:
            line = f"{show(r['current'])} -> {target}"
        print(f"  {r['repo']:50s} {line}")

    current = Counter(show(r["current"]) for r in ok)
    print(f"\nYou maintain {len(repos)} feedstocks.")
    print("  Current automerge: " + ", ".join(f"{k}={v}" for k, v in sorted(current.items())))
    print(f"  Will change to '{target}': {len(todo)}")
    print(f"  Already '{target}':      {len(ok) - len(todo)}")
    if errors:
        print(f"  Errors:              {len(errors)}")

    if not todo:
        print("\nNothing to do.")
        return
    if a.dry_run:
        print("\n--- diffs ---")
        for r in todo:
            sys.stdout.writelines(difflib.unified_diff(
                r["old"].splitlines(True), r["new"].splitlines(True),
                f"{r['repo']}/{CFG}", f"{r['repo']}/{CFG}"))
        print("\nDry run, nothing pushed.")
        return
    if not a.yes and input(f"\nPush to {len(todo)} feedstocks? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return

    failed = 0
    for i, r in enumerate(todo, 1):
        try:
            apply(r, msg)
            status = "done"
        except Exception as e:
            status, failed = f"ERROR: {e}", failed + 1
        print(f"  [{i}/{len(todo)}] {r['repo']:50s} {status}")
    print(f"\n{len(todo) - failed} updated, {failed} failed.")
    sys.exit(1 if failed or errors else 0)


if __name__ == "__main__":
    main()
