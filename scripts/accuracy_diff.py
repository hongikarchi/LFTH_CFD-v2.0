"""Compare trails.json from a candidate run against the saved baseline.

Pairs trails by Idp, then reports:
  - endpoint Euclidean displacement (mean, p50, p90, max)
  - common-frame trajectory L2 divergence
  - catch_rate / touch_all_ratio delta (from result.json)
"""
import json
import math
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
BASELINE = PROJECT / "runs" / "_baseline_trails.json"
BASE_RESULT = PROJECT / "runs" / "_baseline_result.json"


def percentile(vals, p):
    if not vals: return 0.0
    s = sorted(vals)
    k = int(len(s) * p)
    return s[min(k, len(s) - 1)]


def compare(candidate_dir: Path) -> dict:
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    cand = json.loads((candidate_dir / "trails.json").read_text(encoding="utf-8"))
    base_res = json.loads(BASE_RESULT.read_text(encoding="utf-8"))
    cand_res = json.loads((candidate_dir / "result.json").read_text(encoding="utf-8"))

    # Pair by closest START position (Idp shifts when boundary count changes).
    cand_items = [(k, v[0]) for k, v in cand.items() if v]
    endpoint_ds = []
    path_l2 = []
    used = set()
    for bk, bp in base.items():
        if not bp: continue
        bs = bp[0]
        best = None; bd = 1e18
        for ck, cs in cand_items:
            if ck in used: continue
            d = (bs[0]-cs[0])**2 + (bs[1]-cs[1])**2 + (bs[2]-cs[2])**2
            if d < bd: bd = d; best = ck
        if best is None or bd > (0.5) ** 2:   # start positions must be within 0.5 m
            continue
        used.add(best)
        b = base[bk]; c = cand[best]
        be = b[-1]; ce = c[-1]
        ed = math.sqrt((be[0]-ce[0])**2 + (be[1]-ce[1])**2 + (be[2]-ce[2])**2)
        endpoint_ds.append(ed)
        n = min(len(b), len(c))
        if n >= 2:
            sq = 0.0
            for i in range(n):
                sq += ((b[i][0]-c[i][0])**2 + (b[i][1]-c[i][1])**2 + (b[i][2]-c[i][2])**2)
            path_l2.append(math.sqrt(sq / n))

    return {
        "n_baseline": len(base),
        "n_candidate": len(cand),
        "n_paired": len(endpoint_ds),
        "endpoint_m_mean": sum(endpoint_ds) / len(endpoint_ds) if endpoint_ds else None,
        "endpoint_m_p50": percentile(endpoint_ds, 0.50),
        "endpoint_m_p90": percentile(endpoint_ds, 0.90),
        "endpoint_m_max": max(endpoint_ds) if endpoint_ds else None,
        "path_l2_m_mean": sum(path_l2) / len(path_l2) if path_l2 else None,
        "catch_rate_base": base_res.get("catch_rate_moved", 0),
        "catch_rate_cand": cand_res.get("catch_rate_moved", 0),
        "touch_all_base": base_res.get("touch", {}).get("touch_all_ratio", 0),
        "touch_all_cand": cand_res.get("touch", {}).get("touch_all_ratio", 0),
        "wall_base_s": base_res.get("wall_time_s"),
        "wall_cand_s": cand_res.get("wall_time_s"),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: accuracy_diff.py runs/iter_test_NN")
        sys.exit(2)
    out = compare(Path(sys.argv[1]))
    print(json.dumps(out, indent=2))
