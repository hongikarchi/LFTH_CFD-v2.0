"""Render settings_compare.html from runs/_settings_log.jsonl.

Each FluidX3D experiment (via fx3d_run.py) appends
one JSON line capturing settings + key metrics. This script produces a single
HTML page with:

  - sortable table (click column headers)
  - filter input
  - best-score row highlighted
  - 3 scatter plots: dp vs score, timemax vs score, nozzle_LPM vs score
  - representative PNG thumbnail per run (last frame from frames_dir)

No build step required. Chart.js loaded from CDN.

Run: python scripts/update_settings_compare.py
Output: settings_compare.html at repo root.
"""
from __future__ import annotations

import base64
import html
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SETTINGS_LOG = PROJECT / "runs" / "_settings_log.jsonl"
OUT = PROJECT / "settings_compare.html"


def load_entries() -> list[dict]:
    if not SETTINGS_LOG.exists():
        return []
    out = []
    for line in SETTINGS_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def last_frame_thumbnail_uri(frames_dir: str | None, max_bytes: int = 250_000) -> str:
    """Read latest PNG in frames_dir, return data: URI. Empty if missing/too big."""
    if not frames_dir:
        return ""
    p = Path(frames_dir)
    if not p.is_dir():
        return ""
    pngs = sorted(p.glob("*.png"))
    if not pngs:
        return ""
    last = pngs[-1]
    data = last.read_bytes()
    if len(data) > max_bytes:
        # downsample by skipping every Nth byte is unsafe; just skip thumbnail
        # for huge frames -- user can open the file directly
        return ""
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def fmt(v, ndigits: int = 4) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.{ndigits}f}".rstrip("0").rstrip(".")
    return html.escape(str(v))


COLUMNS = [
    ("ts", "timestamp", 0),
    ("test_id", "test_id", 0),
    ("dp_m", "dp [m]", 4),
    ("timemax_s", "timemax [s]", 2),
    ("n_nozzles", "nozzles", 0),
    ("nozzle_LPM", "LPM", 1),
    ("score", "score", 4),
    ("in_positive", "in_pos", 0),
    ("in_negative", "in_neg", 0),
    ("in_column", "in_col", 0),
    ("splash", "splash", 0),
    ("total", "total", 0),
    ("wall_s", "wall [s]", 1),
]


def render(entries: list[dict]) -> str:
    if not entries:
        body = "<p>No experiments logged yet. Run <code>python scripts/fx3d_run.py</code>.</p>"
    else:
        # best-score row
        scored = [(i, e.get("score") or 0.0) for i, e in enumerate(entries)]
        best_i = max(scored, key=lambda x: x[1])[0]

        thead = "".join(f'<th data-key="{k}">{label}</th>' for k, label, _ in COLUMNS) + "<th>preview</th>"
        rows = []
        for i, e in enumerate(entries):
            cls = ' class="best"' if i == best_i else ""
            cells = "".join(f'<td data-key="{k}" data-num="{e.get(k) if isinstance(e.get(k), (int,float)) else ""}">{fmt(e.get(k), nd)}</td>'
                            for k, _, nd in COLUMNS)
            thumb = last_frame_thumbnail_uri(e.get("frames_dir"))
            preview = (f'<a href="{html.escape(e.get("frames_dir") or "#")}">'
                       f'<img src="{thumb}" loading="lazy"/></a>' if thumb else
                       f'<a href="{html.escape(e.get("frames_dir") or "#")}">open</a>')
            rows.append(f'<tr{cls}>{cells}<td class="thumb">{preview}</td></tr>')
        table = (f'<table id="t"><thead><tr>{thead}</tr></thead>'
                 f'<tbody>{"".join(rows)}</tbody></table>')

        chart_data = json.dumps([{
            "test_id": e.get("test_id"),
            "dp_m": e.get("dp_m"), "timemax_s": e.get("timemax_s"),
            "nozzle_LPM": e.get("nozzle_LPM"), "score": e.get("score"),
            "wall_s": e.get("wall_s"),
        } for e in entries])

        body = f"""
<div class="bar">
  <input id="q" placeholder="filter (case-insensitive)" />
  <span class="hint">{len(entries)} runs · best score row highlighted · click headers to sort</span>
</div>
{table}
<div class="charts">
  <div><h3>dp vs score</h3><canvas id="c_dp"></canvas></div>
  <div><h3>timemax vs score</h3><canvas id="c_t"></canvas></div>
  <div><h3>LPM vs score</h3><canvas id="c_l"></canvas></div>
  <div><h3>wall vs score</h3><canvas id="c_w"></canvas></div>
</div>
<script>
const D = {chart_data};
function mkScatter(canvasId, key) {{
  const ctx = document.getElementById(canvasId);
  const pts = D.filter(d => d[key] !== null && d.score !== null)
               .map(d => ({{x: d[key], y: d.score, label: d.test_id}}));
  new Chart(ctx, {{
    type: 'scatter',
    data: {{datasets: [{{data: pts, backgroundColor: '#1d6ddc', pointRadius: 5}}]}},
    options: {{
      plugins: {{tooltip: {{callbacks: {{label: c => c.raw.label + ': (' + c.raw.x + ', ' + c.raw.y.toFixed(4) + ')'}}}}, legend: {{display: false}}}},
      scales: {{x: {{title: {{display: true, text: key}}}}, y: {{title: {{display: true, text: 'score'}}, min: 0, max: 1}}}}
    }}
  }});
}}
mkScatter('c_dp', 'dp_m');
mkScatter('c_t', 'timemax_s');
mkScatter('c_l', 'nozzle_LPM');
mkScatter('c_w', 'wall_s');

// sortable table
document.querySelectorAll('#t thead th').forEach((th, idx) => {{
  let asc = false;
  th.addEventListener('click', () => {{
    const rows = Array.from(document.querySelectorAll('#t tbody tr'));
    rows.sort((a, b) => {{
      const av = a.cells[idx].dataset.num || a.cells[idx].innerText;
      const bv = b.cells[idx].dataset.num || b.cells[idx].innerText;
      const an = parseFloat(av), bn = parseFloat(bv);
      const cmp = (!isNaN(an) && !isNaN(bn)) ? an - bn : av.localeCompare(bv);
      return asc ? cmp : -cmp;
    }});
    asc = !asc;
    const tbody = document.querySelector('#t tbody');
    rows.forEach(r => tbody.appendChild(r));
  }});
}});

// filter
document.getElementById('q').addEventListener('input', e => {{
  const q = e.target.value.toLowerCase();
  document.querySelectorAll('#t tbody tr').forEach(r => {{
    r.style.display = r.innerText.toLowerCase().includes(q) ? '' : 'none';
  }});
}});
</script>
"""
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>LFTH_CFD · settings compare</title>
<meta http-equiv="refresh" content="60">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {{ --bg:#0f1115; --panel:#1a1d27; --text:#e7e9ef; --muted:#8a93a6; --border:#2a3040; --accent:#7cc4ff; --best:#2a4030; }}
@media (prefers-color-scheme: light) {{
  :root {{ --bg:#f6f8fb; --panel:#fff; --text:#1a1e29; --muted:#5b6273; --border:#d6dbe5; --accent:#1d6ddc; --best:#d9f3df; }}
}}
body {{ font-family: -apple-system, "Segoe UI", "Pretendard", "Noto Sans KR", sans-serif;
       background: var(--bg); color: var(--text); margin: 0; padding: 24px; line-height: 1.5; }}
h1 {{ margin: 0 0 8px 0; font-size: 22px; }}
.bar {{ display: flex; align-items: center; gap: 16px; margin-bottom: 12px; }}
.bar input {{ padding: 6px 10px; background: var(--panel); color: var(--text); border: 1px solid var(--border); border-radius: 4px; flex: 0 0 280px; }}
.hint {{ color: var(--muted); font-size: 12px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; background: var(--panel); }}
th, td {{ padding: 6px 9px; border-bottom: 1px solid var(--border); text-align: right; vertical-align: middle; }}
th:nth-child(1), td:nth-child(1), th:nth-child(2), td:nth-child(2) {{ text-align: left; }}
th {{ background: var(--panel); color: var(--muted); font-size: 11px; text-transform: uppercase;
      letter-spacing: .06em; cursor: pointer; user-select: none; position: sticky; top: 0; }}
tr.best td {{ background: var(--best); font-weight: 600; }}
tr:hover td {{ background: var(--border); }}
.thumb img {{ width: 110px; height: auto; border-radius: 3px; vertical-align: middle; }}
.charts {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 24px; margin-top: 28px; max-width: 1200px; }}
.charts h3 {{ margin: 0 0 8px 0; font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }}
canvas {{ background: var(--panel); border: 1px solid var(--border); border-radius: 6px; max-height: 320px; }}
code {{ background: var(--panel); padding: 2px 6px; border-radius: 3px; }}
</style>
</head>
<body>
<h1>LFTH_CFD · settings compare</h1>
{body}
</body>
</html>
"""


def main() -> int:
    entries = load_entries()
    html_text = render(entries)
    OUT.write_text(html_text, encoding="utf-8")
    print(f"wrote {OUT}  ({len(entries)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
