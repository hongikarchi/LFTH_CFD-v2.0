"""
GA dashboard generator.

Reads experiments/sequential_state.json and renders:
  - per-stage fitness evolution chart (generation by generation)
  - cumulative best per stage
  - gene value scatter (parallel coordinates)
  - best individual summary table

Output: ga_dashboard.html at repo root. Auto-refresh meta 20s.

Run:
    python scripts/update_ga_dashboard.py
Or hook into experiment_runner.py to call after each eval.
"""
from __future__ import annotations

import base64
import io
import json
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
STATE = PROJECT / "experiments" / "sequential_state.json"
OUT = PROJECT / "ga_dashboard.html"

POP = 8           # must match ga_sequential.py
N_GEN = 4
GENE_ORDER = ["tx", "ty", "tz", "rx", "ry", "rz", "scale"]
GENE_BOUNDS = {
    "tx": (-5, 5), "ty": (-5, 5), "tz": (-3, 3),
    "rx": (-30, 30), "ry": (-30, 30), "rz": (-30, 30),
    "scale": (0.6, 1.4),
}


def png_data_uri(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def fitness_evolution_chart(stage_idx: int, evals: list, best_so_far: float) -> str:
    """Line chart: per-eval fitness, generation shading, best-so-far line."""
    if not evals:
        return ""
    n = len(evals)
    x = np.arange(n)
    fits = np.array([e["fitness"] for e in evals], dtype=float)
    # Generation bands: each generation has POP evals
    fig, ax = plt.subplots(figsize=(8, 4), facecolor="#161922")
    ax.set_facecolor("#0f1115")
    # Per-generation background
    for g in range(N_GEN):
        ax.axvspan(g * POP - 0.5, (g + 1) * POP - 0.5,
                   alpha=0.06, color=("#7cc4ff" if g % 2 == 0 else "#5dd39e"))
    # Eval points
    colors = ["#ff7a59" if f >= 1e5 else "#7cc4ff" for f in fits]
    ax.scatter(x, fits, c=colors, s=50, zorder=3, edgecolor="white", linewidth=0.5)
    # Cumulative best
    cum_best = np.minimum.accumulate(np.where(fits >= 1e5, np.inf, fits))
    ax.plot(x, cum_best, "-", color="#5dd39e", linewidth=2, label="best so far", zorder=2)

    # Generation labels
    for g in range(N_GEN):
        cx = g * POP + POP / 2 - 0.5
        ax.text(cx, ax.get_ylim()[1] * 0.95 if ax.get_ylim()[1] > 0 else 0,
                f"gen {g}", ha="center", color="#9aa3b2", fontsize=9, weight="bold")

    ax.set_xlabel("eval #", color="#e6e8ee")
    ax.set_ylabel("fitness (lower = better)", color="#e6e8ee")
    ax.set_title(f"Stage {stage_idx} (module {stage_idx}) — fitness evolution",
                  color="#e6e8ee", fontsize=12, weight="bold")
    ax.tick_params(colors="#9aa3b2")
    for spine in ax.spines.values():
        spine.set_color("#2a3040")
    ax.legend(loc="upper right", facecolor="#1e2230", edgecolor="#2a3040",
              labelcolor="#e6e8ee", framealpha=0.9)
    ax.grid(True, alpha=0.15, color="#2a3040")
    return png_data_uri(fig)


def gene_parallel_chart(stage_idx: int, evals: list) -> str:
    """Parallel coordinates of gene values, colored by fitness."""
    if not evals:
        return ""
    fig, ax = plt.subplots(figsize=(9, 4), facecolor="#161922")
    ax.set_facecolor("#0f1115")
    n_dim = len(GENE_ORDER)
    fits = [e["fitness"] for e in evals]
    valid_fits = [f for f in fits if f < 1e5]
    if not valid_fits:
        return ""
    fmin, fmax = min(valid_fits), max(valid_fits)
    cmap = plt.get_cmap("viridis")
    for e in evals:
        f = e["fitness"]
        if f >= 1e5:
            continue
        ind = e["ind"]
        # Normalize each gene to [0,1] for plotting
        ys = []
        for g in GENE_ORDER:
            lo, hi = GENE_BOUNDS[g]
            v = (ind[g] - lo) / (hi - lo)
            ys.append(v)
        norm = (f - fmin) / (fmax - fmin + 1e-9)
        color = cmap(1 - norm)   # better fitness (lower) = lighter
        ax.plot(range(n_dim), ys, color=color, alpha=0.7, linewidth=1.5)
    ax.set_xticks(range(n_dim))
    ax.set_xticklabels(GENE_ORDER, color="#e6e8ee")
    ax.set_ylabel("normalized [0,1]", color="#e6e8ee")
    ax.set_title(f"Stage {stage_idx} — gene space (color = fitness, lighter = better)",
                 color="#e6e8ee", fontsize=11, weight="bold")
    ax.tick_params(colors="#9aa3b2")
    for spine in ax.spines.values():
        spine.set_color("#2a3040")
    ax.grid(True, alpha=0.15, color="#2a3040")
    ax.set_ylim(-0.05, 1.05)
    return png_data_uri(fig)


def render_html(state: dict) -> str:
    evaluations = state.get("evaluations", [])
    stages_done = state.get("stages", {})

    # Group evaluations by stage
    by_stage: dict[int, list] = {}
    for e in evaluations:
        by_stage.setdefault(e["stage"], []).append(e)

    blocks = []
    for stage_idx in sorted(by_stage.keys()):
        evs = by_stage[stage_idx]
        valid_fits = [e["fitness"] for e in evs if e["fitness"] < 1e5]
        best = min(valid_fits) if valid_fits else None
        n = len(evs)
        target = POP * N_GEN
        progress_pct = min(100, int(100 * n / target))
        done = stage_idx in [int(k) for k in stages_done]
        status = "✓ done" if done else f"running ({n}/{target} evals · {progress_pct}%)"
        best_ind = None
        if str(stage_idx) in stages_done:
            best_ind = stages_done[str(stage_idx)].get("best_params")
        else:
            # try to find best so far
            valid = [e for e in evs if e["fitness"] < 1e5]
            if valid:
                best_e = min(valid, key=lambda e: e["fitness"])
                best_ind = best_e["ind"]

        chart_fit = fitness_evolution_chart(stage_idx, evs, best or 0)
        chart_gene = gene_parallel_chart(stage_idx, evs)

        best_str = "<em>n/a</em>"
        if best_ind:
            best_str = (
                f"<code>T=({best_ind['tx']:+.2f}, {best_ind['ty']:+.2f}, {best_ind['tz']:+.2f}) m</code> · "
                f"<code>R=({best_ind['rx']:+.1f}°, {best_ind['ry']:+.1f}°, {best_ind['rz']:+.1f}°)</code> · "
                f"<code>s={best_ind['scale']:.2f}</code>"
            )

        block = f"""
        <section class="stage">
          <h2>Stage {stage_idx} — module {stage_idx} {('(top)' if stage_idx == 0 else ('(bottom)' if stage_idx == 5 else ''))}</h2>
          <div class="meta">
            <span class="badge">{status}</span>
            {f'<span class="badge good">best = {best:.3f}</span>' if best is not None else ''}
            <span class="badge">n_evals = {n}</span>
          </div>
          <div class="best-line">best individual so far: {best_str}</div>
          <img src="{chart_fit}" alt="fitness evolution" />
          <img src="{chart_gene}" alt="gene parallel coords" />
        </section>
        """
        blocks.append(block)

    if not blocks:
        blocks = ["<p class='note'>아직 GA 평가가 없음. <code>python scripts/ga_sequential.py</code> 실행 후 새로고침.</p>"]

    # All-stage summary
    summary_rows = []
    for k, v in sorted(stages_done.items(), key=lambda x: int(x[0])):
        bp = v.get("best_params", {})
        summary_rows.append(
            f"<tr><td>module {k}</td>"
            f"<td>{v.get('best_fitness', '—')}</td>"
            f"<td><code>{json.dumps(bp)}</code></td></tr>"
        )
    summary = ""
    if summary_rows:
        summary = (
            "<section><h2>완료된 stages — best params</h2>"
            "<table><tr><th>module</th><th>best fitness</th><th>params</th></tr>"
            + "".join(summary_rows) + "</table></section>"
        )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="20">
<title>GA Dashboard · LFTH_CFD v2.0</title>
<style>
  :root {{ --bg:#0f1115; --panel:#161922; --panel2:#1e2230; --text:#e6e8ee; --muted:#9aa3b2;
          --accent:#7cc4ff; --green:#5dd39e; --red:#ff7a59; --border:#2a3040; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
          font-family:-apple-system,"Segoe UI","Pretendard","Noto Sans KR",sans-serif;
          line-height:1.5; padding:24px 32px; }}
  h1 {{ margin:0 0 6px 0; font-size:24px; }}
  h2 {{ margin:14px 0 8px 0; font-size:17px; border-bottom:1px solid var(--border); padding-bottom:5px; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:18px; }}
  .links a {{ color:var(--accent); margin-right:14px; text-decoration:none; font-size:13px; }}
  section.stage {{ background:var(--panel); border:1px solid var(--border);
                    border-radius:8px; padding:14px 18px; margin:14px 0; }}
  .meta {{ display:flex; gap:8px; margin-bottom:8px; flex-wrap:wrap; }}
  .badge {{ background:var(--panel2); border:1px solid var(--border); color:var(--muted);
            padding:3px 10px; border-radius:99px; font-size:12px; }}
  .badge.good {{ color:var(--green); border-color:var(--green); }}
  .best-line {{ font-size:13px; color:var(--muted); margin:6px 0 10px 0; }}
  code {{ background:var(--panel2); padding:1px 5px; border-radius:3px; font-size:12px; }}
  img {{ max-width:100%; height:auto; display:block; margin:8px 0; border:1px solid var(--border); border-radius:6px; }}
  table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:13px; }}
  th, td {{ padding:7px 10px; border-bottom:1px solid var(--border); text-align:left; }}
  th {{ background:var(--panel2); color:var(--muted); font-size:11px; text-transform:uppercase; }}
  .note {{ color:var(--muted); font-style:italic; padding:18px; }}
  .ts {{ text-align:center; color:var(--muted); font-size:11px; margin-top:24px; }}
</style>
</head>
<body>
<h1>GA Dashboard</h1>
<div class="sub">Sequential per-module DEAP GA · pop {POP} × gen {N_GEN} · auto-refresh 20s</div>
<div class="links">
  <a href="experiments.html">전체 실험 dashboard</a>
  <a href="ARCHITECTURE.html">Architecture</a>
  <a href="EXPERIMENT_PROTOCOL.md">Protocol</a>
</div>

{''.join(blocks)}

{summary}

<div class="ts">last refreshed {time.strftime('%Y-%m-%d %H:%M:%S')}</div>
</body>
</html>
"""


def main():
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    html = render_html(state)
    OUT.write_text(html, encoding="utf-8")
    n = len(state.get("evaluations", []))
    print(f"Wrote {OUT}  (evals={n})")


if __name__ == "__main__":
    main()
