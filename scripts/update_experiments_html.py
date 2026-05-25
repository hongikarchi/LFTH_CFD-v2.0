"""
Regenerate experiments.html dashboard from all runs/iter_test_*/result.json files.
"""
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
RUNS = PROJECT / "runs"
HTML_OUT = PROJECT / "experiments.html"


HEAD = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>LFTH_CFD v2.0 — Experiments Dashboard</title>
<style>
  :root { --bg:#0f1115; --panel:#161922; --panel2:#1e2230; --text:#e6e8ee; --muted:#9aa3b2;
          --accent:#7cc4ff; --green:#5dd39e; --red:#ff7a59; --border:#2a3040; }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f6f8fb; --panel:#fff; --panel2:#eef1f6; --text:#1a1e29; --muted:#5b6273;
            --accent:#1d6ddc; --green:#1f8b56; --red:#d24a2a; --border:#d6dbe5; }
  }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family:-apple-system,"Segoe UI","Pretendard","Noto Sans KR",sans-serif;
         line-height:1.5; padding:32px; }
  h1 { margin:0 0 6px 0; font-size:26px; }
  .sub { color:var(--muted); margin-bottom:18px; font-size:14px; }
  .links a { color:var(--accent); margin-right:14px; text-decoration:none; font-size:13px; }
  table { width:100%; border-collapse:collapse; margin-top:18px; font-size:13px; }
  th, td { padding:9px 11px; border-bottom:1px solid var(--border); text-align:left; vertical-align:top; }
  th { background:var(--panel2); color:var(--muted); font-size:11px;
       text-transform:uppercase; letter-spacing:.06em; }
  tr:hover td { background:var(--panel2); }
  .ratio { font-weight:600; }
  .ratio.good { color:var(--green); }
  .ratio.ok { color:var(--accent); }
  .ratio.bad { color:var(--red); }
  details { background:var(--panel); border:1px solid var(--border); border-radius:6px;
            padding:6px 12px; margin-top:4px; }
  details summary { cursor:pointer; font-size:12px; color:var(--muted); }
  details pre { font-family:"JetBrains Mono",Consolas,monospace; font-size:11px;
                background:var(--bg); padding:8px; border-radius:4px;
                overflow-x:auto; margin:6px 0 0 0; }
  .note { color:var(--muted); font-style:italic; max-width:300px; }
  .meta { font-size:11px; color:var(--muted); margin-top:24px; }
</style>
</head>
<body>
<h1>Experiments Dashboard</h1>
<div class="sub">CFD-driven collider tuning — each row is one test_NN run.</div>
<div class="links">
  <a href="EXPERIMENT_PROTOCOL.md">Protocol</a>
  <a href="ARCHITECTURE.html">Architecture</a>
  <a href="PLAN.md">Plan</a>
  <a href="https://github.com/hongikarchi/LFTH_CFD-v2.0">GitHub</a>
</div>
"""

TAIL_FMT = """<div class="meta">Last refreshed: {ts}</div>
</body></html>
"""


def ratio_class(r):
    if r < 0.5: return "good"
    if r < 0.8: return "ok"
    return "bad"


def module_summary(modules):
    """One-liner summary of which modules got non-identity transforms."""
    if not modules: return "—"
    out = []
    for m in modules:
        idx = m.get("index")
        rot = m.get("rotation_deg", [0, 0, 0])
        trans = m.get("translation_m", [0, 0, 0])
        scl = m.get("scale", 1.0)
        if any(rot) or any(trans) or scl != 1.0:
            tag = []
            if any(rot): tag.append("R" + ",".join(f"{r:g}" for r in rot))
            if any(trans): tag.append("T" + ",".join(f"{t:g}" for t in trans))
            if scl != 1.0: tag.append(f"S{scl:g}")
            out.append(f"M{idx}[{' '.join(tag)}]")
    return ", ".join(out) if out else "identity"


def regenerate():
    rows = []
    for iter_dir in sorted(RUNS.glob("iter_test_*")):
        rj = iter_dir / "result.json"
        pj = iter_dir / "params.json"
        if not rj.exists(): continue
        r = json.loads(rj.read_text(encoding="utf-8"))
        p = json.loads(pj.read_text(encoding="utf-8")) if pj.exists() else {}
        rows.append((iter_dir.name, r, p))

    # Sort by test_id ascending
    rows.sort(key=lambda x: x[0])

    body = ['<table>']
    body.append("<tr>"
                "<th>Test</th><th>Note</th><th>Modules changed</th>"
                "<th>Caught</th><th>Splash</th><th>Total</th>"
                "<th>Splash ratio</th><th>Wall</th><th>Date</th>"
                "<th>Details</th></tr>")
    for name, r, p in rows:
        tid = r.get("test_id", name)
        ratio = r.get("splash_ratio", 1.0)
        cls = ratio_class(ratio)
        note = p.get("note", "")
        modules = p.get("modules", [])
        body.append(
            f"<tr>"
            f"<td><strong>{tid}</strong></td>"
            f"<td class='note'>{note}</td>"
            f"<td><code>{module_summary(modules)}</code></td>"
            f"<td>{r.get('caught', 0)}</td>"
            f"<td>{r.get('splash', 0)}</td>"
            f"<td>{r.get('total', 0)}</td>"
            f"<td class='ratio {cls}'>{ratio:.3f}</td>"
            f"<td>{r.get('wall_time_s', '—')}s</td>"
            f"<td>{r.get('timestamp', '—')}</td>"
            f"<td><details><summary>params</summary>"
            f"<pre>{json.dumps(p, indent=2, ensure_ascii=False)}</pre>"
            f"</details></td>"
            f"</tr>"
        )
    body.append("</table>")

    if not rows:
        body = ["<p class='note'>No experiments yet. Run "
                "<code>python scripts/experiment_runner.py experiments/test_03.json</code> "
                "to add one.</p>"]

    import time as _t
    html = HEAD + "\n".join(body) + TAIL_FMT.format(ts=_t.strftime("%Y-%m-%d %H:%M:%S"))
    HTML_OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {HTML_OUT}  rows={len(rows)}")
    return HTML_OUT


if __name__ == "__main__":
    regenerate()
