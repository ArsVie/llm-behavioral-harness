"""P4: build the spike-2 lexical codebooks from the measured ev_bins distributions.

Contract: experiments/emotion-codebook-spike2/docs/exp-affect-codebook-spike2-2026-08-16.md
— P4 "build codebook (0.01 grid, smoothing, 3-field artifact)". The brief's P4
section names no mechanics, so this build implements the pre-registered
orchestration decisions (task brief): 0.01 value grid (101 nodes per model per
axis, valence and arousal separate), NO fresh sampling — the 10 measured bins
per axis are the only data source; node candidate tokens are drawn from the
measured per-bin top-30 lists and interpolated across adjacent bins; one
3-field artifact per node.

3-field artifact per node (interpretation, labeled — the contract names the
"3-field artifact" but not its fields):
  1. "value"      — node coordinate on the [0,1] axis grid (float, 0.01 grid).
  2. "candidates" — list of {"token", "prob"}; prob = probability blended from
                    the contributing measured bins' top-30 lists (linear blend
                    of measured probabilities — RE-DERIVED, never re-sampled).
                    Sum(prob) <= 1: only the top-30 non-whitespace tokens per
                    bin were saved during extraction, the residual vocabulary
                    mass is unmeasured and not fabricated.
  3. "provenance" — {"source_bins": [contributing measured bin indices],
                     "weights": {bin index -> blend weight},
                     "n": measured items behind the node = sum of the
                     contributing bins' n}.

Interpolation rule (documented, deterministic, piecewise-linear):
  - Grid index j in 0..100, value v = j/100 (integer-exact arithmetic).
  - Containing measured cell i = min(9, j // 10); cell fraction r = j % 10.
  - Blend weight w = r/10. Node v blends measured bin i with weight (1-w) and
    measured bin i+1 with weight w, EXCEPT:
      * r == 0  -> single contributing bin i (pure measured bin, w = 0);
      * i == 9 and r > 0 -> clamp to measured bin 9 (there is no bin 10;
        no extrapolation; nodes in [0.90, 1.00] are the last measured bin).
  - p_t(v) = (1-w)*p_t(bin_i) + w*p_t(bin_{i+1}); candidate set = union of the
    two contributing bins' top-30 lists (<= 60 candidates). Zero-weight bins
    contribute nothing (at r == 0 the i+1 list is not merged in).
  - Deterministic: pure arithmetic over measured floats; additionally the
    stage seeds seed_everything(derive_seed(MASTER_SEED, 8)) per the
    pre-registered seeding contract (recorded in each artifact's meta).

Quality checks built in (labeled DIAGNOSTIC — gate records are P5's):
  - zero empty candidate lists; every candidate token present in at least one
    measured bin of that axis (provenance violation count); probs in [0,1].
  - verifies = rebuilds the same model's artifacts in memory and byte-compares
    against the on-disk files (rebuild determinism).
  - monotonic valence word-family trend: Spearman(node value, positive-mass
    minus negative-mass) per model (diagnostic; not tuned against).

Usage (CPU-only; no model loading; no network; stdlib only):
  python scripts/build_codebook.py --model qwen|gemma|qwen8b
  python scripts/build_codebook.py --model qwen|gemma|qwen8b --check
  python scripts/build_codebook.py --summary        (compose diagnostics table)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

SPIKE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SPIKE_ROOT))

from harness.determinism import MASTER_SEED, derive_seed, seed_everything  # noqa: E402

#: Stage keys continue the extraction scripts' SEED_KEY numbering
#: (bringup=1, p2a=2, p2b=3, p3=4, boot=5, sample=6, c1=7) — next free: 8.
SEED_KEY: dict[str, int] = {"p4": 8}

MODELS = ("qwen", "gemma", "qwen8b")
AXES = ("valence", "arousal")

EXTRACTIONS = SPIKE_ROOT / "data" / "extractions"
CODEBOOKS = SPIKE_ROOT / "data" / "codebooks"
DIAGNOSTICS = SPIKE_ROOT / "diagnostics"

N_NODES = 101          # 0.00 .. 1.00 in steps of 0.01
N_BINS = 10            # measured bins per axis file (width 0.10)


# ---------------------------------------------------------------------------
# Valence word families — DIAGNOSTIC ONLY (monotonic sanity check; not used to
# build or tune the books). Coarse lemma-root prefix matching over the model's
# subword tokens; fragmentation noise (e.g. "hap" + "py") and neutral
# words sharing a root (e.g. "badge") limit recall — the TREND direction is
# the object, not the absolute mass.
# ---------------------------------------------------------------------------
POSITIVE_ROOTS = (
    "happy", "happi", "joy", "glad", "love", "loving", "great", "good",
    "nice", "hope", "like", "beauti", "wonder", "excit", "peace", "calm",
    "warm", "bright", "smile", "laugh", "pleas", "delight", "cheer",
    "lovely", "fun", "grate",
)
NEGATIVE_ROOTS = (
    "sad", "sorrow", "grief", "anger", "angr", "rage", "fear", "fright",
    "hate", "hatr", "pain", "cry", "crying", "terribl", "awful", "lonely",
    "loneli", "afraid", "anxi", "worr", "depress", "miser", "horribl",
    "upset", "disappoint", "gloom", "dread", "despair", "hurt", "evil",
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_ev_bins(model: str, axis: str) -> tuple[dict, Path, str]:
    src = EXTRACTIONS / model / f"ev_bins_{axis}.json"
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    return data, src, sha256_file(src)


def build_nodes(ev: dict) -> list[dict]:
    """101 nodes from the 10 measured bins (piecewise-linear blend, above)."""
    bins = ev["bins"]
    assert len(bins) == N_BINS, f"expected {N_BINS} measured bins, got {len(bins)}"
    nodes: list[dict] = []
    for j in range(N_NODES):
        i = min(N_BINS - 1, j // 10)
        r = j % 10
        w = r / 10.0
        contributors: list[tuple[int, float]] = [(i, 1.0 - w)]
        if r > 0 and i + 1 < N_BINS:
            contributors.append((i + 1, w))
        probs: dict[str, float] = {}
        for bi, weight in contributors:
            for token, p in bins[bi]["top_tokens"]:
                probs[token] = probs.get(token, 0.0) + weight * p
        candidates = sorted(probs.items(), key=lambda kv: (-kv[1], kv[0]))
        source_bins = [bi for bi, _ in contributors]
        weights = {str(bi): round(wt, 6) for bi, wt in contributors}
        n_behind = sum(bins[bi]["n"] for bi in source_bins)
        nodes.append({
            "value": j / 100.0,
            "candidates": [{"token": t, "prob": p} for t, p in candidates],
            "provenance": {
                "source_bins": source_bins,
                "weights": weights,
                "n": n_behind,
            },
        })
    return nodes


def build_codebook(model: str, axis: str) -> tuple[dict, Path]:
    """Build one codebook artifact; returns (artifact, out_path)."""
    ev, src_path, src_sha = load_ev_bins(model, axis)
    seed = derive_seed(MASTER_SEED, SEED_KEY["p4"])
    seed_everything(seed)
    nodes = build_nodes(ev)
    measured_tokens = {t[0] for b in ev["bins"] for t in b["top_tokens"]}
    n_measured = sum(b["n"] for b in ev["bins"])

    artifact = {
        "artifact": "P4 lexical codebook (spike 2)",
        "artifact_version": 1,
        "model": ev["model"],
        "revision": ev["revision"],
        "axis": axis,
        "grid": {"start": 0.0, "stop": 1.0, "step": 0.01, "n_nodes": len(nodes)},
        "interpolation": {
            "mode": "piecewise-linear blend of adjacent measured bins "
                    "(RE-DERIVED from measured top-30 lists; no fresh sampling)",
            "candidate_source": "union of the contributing measured bins' "
                                "top-30 non-whitespace token lists",
        },
        "fields_per_node": {
            "value": "node coordinate on the [0,1] axis grid (float, 0.01 grid)",
            "candidates": "list of {token, prob}; prob = linear blend of the "
                          "contributing measured bins' probabilities; "
                          "sum(prob) <= 1 (only top-30 measured per bin; "
                          "residual mass unmeasured, not fabricated)",
            "provenance": "{source_bins: contributing measured bin indices, "
                          "weights: blend weight per contributing bin, "
                          "n: measured items behind the node}",
        },
        "source": {
            "file": str(src_path.relative_to(SPIKE_ROOT)),
            "sha256": src_sha,
            "n_bins": len(ev["bins"]),
            "n_measured": n_measured,
            "extraction_seed": ev["seed"],
            "direction_layer": ev["binning"].get("direction_layer"),
            "direction_train_r": ev["binning"].get("direction_train_r"),
        },
        "build": {
            "script": "scripts/build_codebook.py",
            "seed": seed,
            "note": "timestamp-free artifact: rebuild reproduces identical bytes",
        },
        "nodes": nodes,
    }

    # --- in-build quality asserts (diagnostic) -------------------------------
    empty = [n["value"] for n in nodes if not n["candidates"]]
    assert not empty, f"{model}/{axis}: {len(empty)} empty candidate lists"
    violations = [
        (n["value"], c["token"])
        for n in nodes for c in n["candidates"]
        if c["token"] not in measured_tokens
    ]
    assert not violations, f"{model}/{axis}: provenance violations: {violations[:5]}"
    for n in nodes:
        for c in n["candidates"]:
            assert 0.0 <= c["prob"] <= 1.0

    out_dir = CODEBOOKS / model
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{axis}_codebook.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return artifact, out_path


# ---------------------------------------------------------------------------
# Diagnostics (post-build; labeled DIAGNOSTIC, gate records live in P5)
# ---------------------------------------------------------------------------
def _spearman(xs, ys):
    """Spearman rank correlation (stdlib-only; ties get average ranks)."""
    def rank(vals):
        order = sorted(range(len(vals)), key=lambda k: vals[k])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = 1.0 + (i + j) / 2.0
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    if n < 2:
        return float("nan")  # degenerate series (no family mass / constant)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    denom = math.sqrt(vx * vy)
    return (cov / denom) if denom > 0 else float("nan")


def _family_masses(nodes, axis):
    """Per-node probability mass over the POSITIVE/NEGATIVE lemma roots."""
    pos, neg = [], []
    for n in nodes:
        pm = nm = 0.0
        for c in n["candidates"]:
            tok = c["token"].lower()
            if any(tok.startswith(r) for r in POSITIVE_ROOTS):
                pm += c["prob"]
            elif any(tok.startswith(r) for r in NEGATIVE_ROOTS):
                nm += c["prob"]
        pos.append(pm)
        neg.append(nm)
    return pos, neg


def _jensen_shannon(p, q, support):
    """JS divergence over a shared support (probabilities pre-normalized).

    JS(P,Q) = H(M) - (H(P)+H(Q))/2 with M = (P+Q)/2, >= 0 in nats.
    """
    ent = lambda v: -sum(x * math.log(x) for x in v if x > 0)
    m = [(p[t] + q[t]) / 2.0 for t in support]
    return ent(m) - (ent([p[t] for t in support]) + ent([q[t] for t in support])) / 2.0


def node_dist(node, support):
    """Normalized distribution over a shared support (union of two nodes)."""
    base = {c["token"]: c["prob"] for c in node["candidates"]}
    total = sum(base.get(t, 0.0) for t in support)
    return {t: base.get(t, 0.0) / total for t in support}


def diagnostics(model: str, axis: str, artifact: dict) -> dict:
    """Post-build diagnostic summary for one artifact (no gates evaluated)."""
    nodes = artifact["nodes"]
    vals = [n["value"] for n in nodes]
    n_cand = [len(n["candidates"]) for n in nodes]
    pos, neg = _family_masses(nodes, axis)

    # G-SMOOTH preview: adjacent JS on a 0.2-spaced subgrid (5 nodes) and the
    # max adjacent JS on the full 0.01 grid (both diagnostic; gate is P5's).
    js_preview = []
    grid5 = nodes[::20]  # 0.00, 0.20, 0.40, 0.60, 0.80 — last node 1.00 not in this slice
    for a, b in zip(grid5, grid5[1:]):
        support = sorted({c["token"] for c in a["candidates"]} | {c["token"] for c in b["candidates"]})
        js_preview.append(_jensen_shannon(node_dist(a, support), node_dist(b, support), support))
    js_full = []
    for a, b in zip(nodes, nodes[1:]):
        support = sorted({c["token"] for c in a["candidates"]} | {c["token"] for c in b["candidates"]})
        js_full.append(_jensen_shannon(node_dist(a, support), node_dist(b, support), support))

    diag = {
        "grid_size": len(nodes),
        "empty_nodes": sum(1 for n in nodes if not n["candidates"]),
        "n_candidates_mean": sum(n_cand) / len(n_cand),
        "n_candidates_min": min(n_cand),
        "n_candidates_max": max(n_cand),
    }
    if axis == "valence":
        # Monotonic word-family trend (DIAGNOSTIC; report, do not tune).
        diff = [p - g for p, g in zip(pos, neg)]
        ratio = [p / (p + g) if (p + g) > 0 else float("nan") for p, g in zip(pos, neg)]
        ratio_clean = [r for r in ratio if not math.isnan(r)]
        vals_clean = [v for v, r in zip(vals, ratio) if not math.isnan(r)]
        diag["monotonic_pos_minus_neg_spearman"] = _spearman(vals, diff)
        diag["monotonic_pos_share_spearman"] = _spearman(vals_clean, ratio_clean)
        diag["pos_mass_at_0_1_0_5_0_9"] = [
            next(round(p, 6) for n, p in zip(nodes, pos) if n["value"] == v)
            for v in (0.1, 0.5, 0.9)
        ]
        diag["neg_mass_at_0_1_0_5_0_9"] = [
            next(round(g, 6) for n, g in zip(nodes, neg) if n["value"] == v)
            for v in (0.1, 0.5, 0.9)
        ]
    diag["js_0_2spacing_max"] = max(js_preview) if js_preview else None
    diag["js_0_2spacing_pairs"] = [round(x, 6) for x in js_preview]
    diag["js_full_grid_max"] = max(js_full) if js_full else None
    return diag


def top_tokens_at(artifact: dict, value: float, k: int = 5) -> list[dict]:
    node = next(n for n in artifact["nodes"] if n["value"] == value)
    return node["candidates"][:k]


def write_summary() -> None:
    """Compose diagnostics/codebook-summary.md from the 6 committed artifacts."""
    rows: list[str] = []
    for model in MODELS:
        for axis in AXES:
            path = CODEBOOKS / model / f"{axis}_codebook.json"
            with open(path, encoding="utf-8") as f:
                art = json.load(f)
            ev, _, _ = load_ev_bins(model, axis)
            measured_tokens = {t[0] for b in ev["bins"] for t in b["top_tokens"]}
            diag = diagnostics(model, axis, art)
            t01 = " ".join(f"{c['token']}({c['prob']:.3f})" for c in top_tokens_at(art, 0.1))
            t05 = " ".join(f"{c['token']}({c['prob']:.3f})" for c in top_tokens_at(art, 0.5))
            t09 = " ".join(f"{c['token']}({c['prob']:.3f})" for c in top_tokens_at(art, 0.9))
            # Computed from the artifacts, not asserted: empty nodes and
            # provenance violations (token absent from every measured bin).
            empty = sum(1 for n in art["nodes"] if not n["candidates"])
            violations = sum(
                1 for n in art["nodes"] for c in n["candidates"]
                if c["token"] not in measured_tokens
            )
            trend = ""
            if axis == "valence":
                s = diag["monotonic_pos_minus_neg_spearman"]
                ss = diag["monotonic_pos_share_spearman"]
                trend = f"rho(pos-neg)={s:.3f}; rho(share)={ss:.3f}"
            rows.append(
                f"| {model} | {axis} | {diag['grid_size']} | "
                f"{diag['n_candidates_mean']:.1f} ({diag['n_candidates_min']}-{diag['n_candidates_max']}) | "
                f"{empty} | {violations} | {art['source']['n_measured']} | "
                f"{diag['js_0_2spacing_max']:.4f} / {diag['js_full_grid_max']:.6f} | "
                f"{trend or 'n/a'} | {t01} | {t05} | {t09} |"
            )
    header = (
        "# P4 lexical codebooks — build summary (diagnostic; gates are P5's)\n\n"
        "Built: scripts/build_codebook.py, CPU-only, no model loading, no fresh "
        "sampling (piecewise-linear blend over the 10 measured bins per axis; "
        "RE-DERIVED probabilities; provenance = measured top-30 lists only).\n\n"
        "| model | axis | grid nodes | candidates/node (min-max) | empty nodes | "
        "provenance violations | measured n | JS max (0.2-spacing / full grid) | "
        "valence monotonic trend | top-5 @ 0.1 | top-5 @ 0.5 | top-5 @ 0.9 |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    DIAGNOSTICS.mkdir(parents=True, exist_ok=True)
    with open(DIAGNOSTICS / "codebook-summary.md", "w", encoding="utf-8") as f:
        f.write(header + "\n".join(rows) + "\n")
    print(f"[summary] wrote {DIAGNOSTICS / 'codebook-summary.md'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="P4 codebook builder (spike 2)")
    ap.add_argument("--model", choices=MODELS, help="model key (qwen|gemma|qwen8b)")
    ap.add_argument("--check", action="store_true",
                    help="rebuild in memory and byte-compare against on-disk artifacts")
    ap.add_argument("--summary", action="store_true",
                    help="compose diagnostics/codebook-summary.md from committed artifacts")
    args = ap.parse_args()

    if args.summary:
        write_summary()
        return

    if args.model is None:
        ap.error("--model is required (or use --summary)")

    t0 = time.monotonic()
    for axis in AXES:
        art, out = build_codebook(args.model, axis)
        diag = diagnostics(args.model, axis, art)
        print(f"[p4] {args.model}/{axis}: {out.relative_to(SPIKE_ROOT)} "
              f"nodes={diag['grid_size']} mean_cand={diag['n_candidates_mean']:.1f} "
              f"empty={diag['empty_nodes']}")
        if axis == "valence":
            print(f"[p4]   valence trend rho(pos-neg)="
                  f"{diag['monotonic_pos_minus_neg_spearman']:.3f} rho(share)="
                  f"{diag['monotonic_pos_share_spearman']:.3f}")

    if args.check:
        for axis in AXES:
            on_disk = (CODEBOOKS / args.model / f"{axis}_codebook.json").read_bytes()
            art, _ = build_codebook(args.model, axis)  # rebuild (deterministic)
            rebuilt = json.dumps(art, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ok = rebuilt.encode("utf-8") == on_disk
            print(f"[p4] --check {args.model}/{axis}: byte-identical rebuild: {ok}")
            if not ok:
                sys.exit(1)
    print(f"[p4] {args.model} done in {time.monotonic() - t0:.2f}s "
          f"(seed={derive_seed(MASTER_SEED, SEED_KEY['p4'])})")


if __name__ == "__main__":
    main()