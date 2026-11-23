"""Check the AI triage prompt against listings you've judged yourself.

    python calibrate.py [runs]     # default 2 runs per listing and provider; costs a few Rappen

Add cases when the AI gets one wrong; run this after every prompt change.
"""
import sys
from concurrent.futures import ThreadPoolExecutor

import scrape
import triage

# listing -> acceptable kitchen ratings (your verdicts)
KITCHEN = {
    "newhome:6196746": {"normal", "large"},      # Trotte Feldbach: long L, open to living room
    "homegate:4003533726": {"normal", "large"},  # same flat on homegate
    "homegate:4003489587": {"tiny", "small"},    # Zollikon: narrow closed room, L with little free worktop
    "homegate:4003538996": {"small"},            # narrow galley
    "homegate:4003532816": {"normal", "large"},  # long granite L
    "newhome:6198234": {"small", "normal"},      # freestanding block with appliance column: not tiny
    "homegate:4003483913": {"unknown", "normal", "large"},  # no kitchen photo: must not be small
    "newhome:6182427": {"tiny"},                 # Thalwil: niche, one short run, 2-zone hob, oven below
}
# listing -> custom criteria that must (True) / must not (False) match
CRITERIA = {
    "homegate:4003533726": {"attic apartment": False},  # only other units in the building are attics
    "homegate:4003530099": {"attic apartment": True},   # "Dachwohnung", sloped ceilings
}


def main(runs=2, models=None):
    """models: optional {"anthropic": ..., "openai": ...} to test other models than the configured ones."""
    cfg = scrape.load_state()["triage"] | {"custom_rules": "attic apartment"}
    if models:
        cfg = cfg | {"models": cfg["models"] | models}
    listings = scrape.load_listings()
    keys = [k for k in {**KITCHEN, **CRITERIA} if k in listings]
    tasks = [(k, p, i) for k in keys for p in ("anthropic", "openai") if triage.api_key(p) for i in range(runs)]

    def go(t):
        try:
            return t, triage.triage_listing(listings[t[0]], cfg, t[1], triage.api_key(t[1]))
        except Exception as e:
            return t, e

    with ThreadPoolExecutor(12) as ex:
        out = list(ex.map(go, tasks))
    ok = total = 0
    for k in keys:
        for (kk, p, _), r in out:
            if kk != k:
                continue
            if isinstance(r, Exception):
                print(f"  {k} [{p}] error: {r}")
                continue
            checks = [(f"kitchen {r['kitchen']}", r["kitchen"] in KITCHEN[k])] if k in KITCHEN else []
            matched = set(scrape.matched_exclusions(r))
            checks += [(f"{c} {'matched' if c in matched else 'not matched'}", (c in matched) == want)
                       for c, want in CRITERIA.get(k, {}).items()]
            for what, good in checks:
                ok, total = ok + good, total + 1
                if not good:
                    print(f"  ✗ {k} [{p}]: {what}")
    print(f"{ok}/{total} checks passed")


if __name__ == "__main__":
    # python calibrate.py [runs] [anthropic-model] [openai-model]
    a = sys.argv[1:]
    main(int(a[0]) if a else 2, {k: v for k, v in zip(("anthropic", "openai"), a[1:3])} or None)
