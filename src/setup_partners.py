"""Set up partner umbrellas on the Projects database.

A partner is one `Partner` select column on Projects — Bear brings Streamside
and True Temper, Telus brings its own, everyone else is a direct client — plus
an `Umbrella` checkbox marking the one row per partner that the partner's *own*
hours get booked against (an allocation's Project is a relation, so a partner
has to exist as a real project to be bookable at all). See docs/partners.md.

Idempotent, so re-run it whenever a partner is added.

    # create the columns (seeded Bear + Telus), then an umbrella row per partner
    python src/setup_partners.py

    # file projects under an umbrella (repeat --partner for a second one)
    python src/setup_partners.py --partner Bear "Streamside OSS" "True Temper"

    # take a project back out from under one
    python src/setup_partners.py --partner "" "Fotosprint"

    # a new partner: the option first, then anything that goes under it
    python src/setup_partners.py --add-partner Acme --partner Acme "Some Project"

Projects are named exactly as they read in Notion; matching is case-insensitive
and a name that matches nothing is reported rather than guessed at, because the
wrong guess here files a client under the wrong agency.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from web import notion_ops as ops  # noqa: E402


def add_partner_option(name: str) -> None:
    """Add a name to the Partner select's options, leaving the rest alone."""
    ds = ops._notion.data_sources.retrieve(ops.PROJECTS_DS)
    prop = ds["properties"].get(ops.PARTNER_PROP) or {}
    options = list((prop.get("select") or {}).get("options", []))
    if any(o["name"] == name for o in options):
        print(f"  {name}: already an option")
        return
    options.append({"name": name})
    ops._notion.data_sources.update(ops.PROJECTS_DS, properties={
        ops.PARTNER_PROP: {"select": {"options": options}},
    })
    ops._partner_cache.update(at=0.0, names=None)
    print(f"  {name}: added")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--add-partner", action="append", default=[], metavar="NAME",
                    help="add a partner option to the Partner column")
    ap.add_argument("--partner", metavar="NAME",
                    help='the partner to file the named projects under ("" = direct)')
    ap.add_argument("projects", nargs="*", metavar="PROJECT",
                    help="project names to file under --partner")
    ap.add_argument("--no-umbrella", action="store_true",
                    help="skip creating the per-partner umbrella project rows")
    args = ap.parse_args()

    if args.projects and args.partner is None:
        ap.error("naming projects needs --partner (use --partner '' to clear one)")

    print("Making sure Projects has the Partner and Umbrella columns…")
    ops.ensure_partner_properties()

    for name in args.add_partner:
        add_partner_option(name)

    partners = ops.list_partners()
    print(f"Partners: {', '.join(partners) or '(none yet)'}")

    if args.projects:
        target = args.partner.strip()
        by_name = {}
        for p in ops.list_projects(active_only=False):
            by_name.setdefault(p["name"].strip().lower(), p)
        print(f"\nFiling {len(args.projects)} project(s) under "
              f"{target or 'no partner (direct)'}…")
        missing = []
        for name in args.projects:
            proj = by_name.get(name.strip().lower())
            if not proj:
                missing.append(name)
                continue
            if (proj.get("partner") or "") == target:
                print(f"  {proj['name']}: already there")
                continue
            ops.set_project_partner(proj["id"], target)
            print(f"  {proj['name']}: {proj.get('partner') or 'direct'} -> {target or 'direct'}")
        for name in missing:
            print(f"  !! no project called {name!r} — check the spelling in Notion")

    if not args.no_umbrella:
        print("\nUmbrella projects (the row a partner's own hours book against)…")
        for name in ops.list_partners():
            row = ops.create_umbrella_project(name)
            print(f"  {name}: {row['id']}")

    print("\nProjects by partner:")
    projects = ops.list_projects(active_only=False)
    for name in ops.list_partners(projects) + [""]:
        under = [p for p in projects if (p.get("partner") or "") == name and p["active"]]
        label = name or "Direct clients"
        print(f"  {label} ({len(under)}): "
              + ", ".join(p["name"] + (" [umbrella]" if p.get("umbrella") else "")
                          for p in sorted(under, key=lambda p: p["name"].lower())))


if __name__ == "__main__":
    main()
