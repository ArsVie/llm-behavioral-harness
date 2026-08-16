#!/usr/bin/env python3
"""Spike-2 P5 quality gates: G-SMOOTH (H2), G-DEGEN (H2d), G-MASK (H5).

Pre-registered conditions (contract Gates table):
  G-SMOOTH/G-DEGEN (quality): adjacent-bin JS <= 0.05; monotone (Spearman >= 0.90);
      descriptors at 0.2/0.5/0.8 pairwise distinct
  G-MASK (hard invariant): zero engine numbers in any assembled prompt

Operationalizations (labeled, mine — same spirit as decision 7 for G-DATA):
  - JS: Jensen-Shannon over the per-node normalized candidate distribution
    (union token universe, zero-fill); adjacent nodes on the 0.01 grid; max
    across the 100 adjacent pairs is reported vs 0.05.
  - Monotone: Spearman between node value and polarity contrast
    (pos-family mass - neg-family mass). Families are DATA-DERIVED as tokens
    whose measured mass concentrates at v>=0.8 (pos) / v<=0.2 (neg) in the
    source ev_bins; a hardcoded small-lexicon variant (builders' great/happy/
    love/lover/pleasant family) is reported as a secondary diagnostic.
  - Descriptor: top-3 candidate tokens joined; distinctness = pairwise
    Jaccard(token set) < 1 across nodes {0.2, 0.5, 0.8}.
  - G-MASK at artifact level (no prompts assembled yet at P5): scan the raw
    codebook JSON for engine-number patterns (m=, g=, arg=, p=, M=, mu=, eta=
    or a VAD numeric triple). Value grid + probabilities are the codebook's
    intended content and are not engine numbers.

Deterministic, CPU-only. Writes diagnostics/gates-p5.json + prints verdicts.
"""
import json, math, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CB = ROOT / "data" / "codebooks"
EVBINS = ROOT / "data" / "extractions"
OUT = ROOT / "diagnostics" / "gates-p5.json"

MODELS = ["qwen", "gemma", "qwen8b"]
AXES = ["valence", "arousal"]

ENGINE_NUM_RE = re.compile(
    r"\b(?:m|g|arg|p|M|mu|eta)\s*=\s*[+-]?\d+(?:\.\d+)?"
    r"|\b\d+\.\d+\s+[+-]?\d+\.\d+\s+[+-]?\d+\.\d+\b"
)


def softmax_normalize(cands):
    s = sum(c["prob"] for c in cands)
    return {c["token"]: c["prob"] / s for c in cands} if s > 0 else {}


def js_div(p, q):
    u = sorted(set(p) | set(q))
    pp = [p.get(t, 0.0) for t in u]
    qq = [q.get(t, 0.0) for t in u]
    m = [(a + b) / 2 for a, b in zip(pp, qq)]
    def kl(x, y):
        return sum(xi * math.log(xi / yi) for xi, yi in zip(x, y) if xi > 0)
    return (kl(pp, m) + kl(qq, m)) / 2


def spearman(xs, ys):
    n = len(xs)
    def rank(v):
        idx = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[idx[j + 1]] == v[idx[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[idx[k]] = avg
            i = j + 1
        return r
    rx, ry = rank(xs), rank(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den > 0 else float("nan")


def data_derived_families(evbin):
    """Tokens whose measured mass concentrates in the extreme bins.

    ev_bins schema: top-level 'bins' = [{bin, center, n, mean_y,
    top_tokens: [[token, prob], ...], fallback}]. A token's 'position' is the
    bin center it was measured in; we take the max probability a token ever
    shows in the extreme regions and keep the strongest tokens there.
    """
    bins = evbin["bins"]
    lo, hi = {}, {}
    for b in bins:
        frac = b.get("center", (b.get("low", 0.0) + b.get("high", 0.0)) / 2)
        for entry in b.get("top_tokens", []):
            tok, p = entry[0], entry[1]
            if frac <= 0.2:
                lo[tok] = max(lo.get(tok, 0.0), p)
            elif frac >= 0.8:
                hi[tok] = max(hi.get(tok, 0.0), p)
    # keep tokens that are pronounced in their extreme region (top half by p)
    def topk(d, k=15):
        return {t for t, _ in sorted(d.items(), key=lambda kv: -kv[1])[:k]}
    return topk(hi), topk(lo)


def node_candidate_map(node):
    return {c["token"]: c["prob"] for c in node["candidates"]}


def run():
    records = {}
    for model in MODELS:
        for axis in AXES:
            cb_file = CB / model / f"{axis}_codebook.json"
            ev_file = EVBINS / model / f"ev_bins_{axis}.json"
            book = json.loads(cb_file.read_text())
            nodes = book["nodes"]
            assert len(nodes) == 101, f"{model}/{axis}: {len(nodes)} nodes"

            # --- G-SMOOTH: adjacent JS ---
            dists = [softmax_normalize(n["candidates"]) for n in nodes]
            adj_js = [js_div(dists[i], dists[i + 1]) for i in range(100)]
            max_adj_js = max(adj_js)
            # 0.2-spaced diagnostic
            spaced_js = [js_div(dists[i], dists[i + 20]) for i in range(0, 81, 20)]

            # --- monotone: data-derived families ---
            ev = json.loads(ev_file.read_text())
            pos_fam, neg_fam = data_derived_families(ev)
            vals, contrast = [], []
            for n in nodes:
                cm = node_candidate_map(n)
                vals.append(n["value"])
                ppos = sum(cm.get(t, 0.0) for t in pos_fam)
                pneg = sum(cm.get(t, 0.0) for t in neg_fam)
                contrast.append(ppos - pneg)
            rho = spearman(vals, contrast)
            # secondary diagnostic: hardcoded small lexicon
            hard_pos = {"great", "happy", "love", "lover", "pleasant"}
            contrast_h = []
            for n in nodes:
                cm = node_candidate_map(n)
                contrast_h.append(sum(cm.get(t, 0.0) for t in hard_pos))
            rho_hard = spearman(vals, contrast_h)

            # --- G-DEGEN: descriptors at 0.2/0.5/0.8 ---
            desc = {}
            for v in (0.2, 0.5, 0.8):
                n = nodes[round(v * 100)]
                tok = [c["token"] for c in n["candidates"][:3]]
                desc[v] = {"tokens": tok, "descriptor": "".join(tok).strip() or " ".join(tok)}
            pairs = [(0.2, 0.5), (0.2, 0.8), (0.5, 0.8)]
            jac = {}
            for a, b in pairs:
                sa, sb = set(desc[a]["tokens"]), set(desc[b]["tokens"])
                jac[f"{a}/{b}"] = len(sa & sb) / len(sa | sb) if (sa | sb) else 1.0
            distinct = all(jac[f"{a}/{b}"] < 1.0 for a, b in pairs)

            # --- G-MASK: engine-number scan (artifact level) ---
            raw = cb_file.read_text()
            eng_hits = sorted(set(ENGINE_NUM_RE.findall(raw)))[:10]
            # allow the value grid + probs: they are floats but not engine numbers;
            # the regex targets m=/g=/arg=/... or a raw 3-float triple
            mask_clean = len(eng_hits) == 0

            smooth_pass = max_adj_js <= 0.05
            mono_pass = rho >= 0.90
            records[f"{model}/{axis}"] = {
                "n_nodes": len(nodes),
                "g_smooth": {"max_adj_js": max_adj_js, "pass_js": smooth_pass,
                             "spaced_js_0_2": spaced_js,
                             "rho_monotone": rho, "pass_monotone": mono_pass,
                             "rho_hard_lexicon": rho_hard,
                             "pos_family": sorted(pos_fam), "neg_family": sorted(neg_fam)},
                "g_degen": {"descriptors": {str(k): v for k, v in desc.items()},
                            "jaccard": jac, "pass_distinct": distinct},
                "g_mask": {"engine_hits": eng_hits, "pass": mask_clean},
                "verdict": {
                    "G-SMOOTH": "PASS" if (smooth_pass and mono_pass) else "FAIL",
                    "G-DEGEN": "PASS" if distinct else "FAIL",
                    "G-MASK": "PASS" if mask_clean else "FAIL",
                },
            }

    OUT.write_text(json.dumps(records, indent=2, ensure_ascii=False))
    print(f"wrote {OUT}")
    for k, r in records.items():
        print(f"{k}: G-SMOOTH {r['verdict']['G-SMOOTH']} (js {r['g_smooth']['max_adj_js']:.4f}, "
              f"rho {r['g_smooth']['rho_monotone']:.3f}) | G-DEGEN {r['verdict']['G-DEGEN']} "
              f"(jac {r['g_degen']['jaccard']}) | G-MASK {r['verdict']['G-MASK']}")
    return 0


if __name__ == "__main__":
    sys.exit(run())