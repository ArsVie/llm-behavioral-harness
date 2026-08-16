"""P6 prompt assembly + invariant checks (emotion-codebook spike 2).

Builds the two behavioral-eval variants on the SAME scaffold, differing ONLY
in the AFFECTIVE BEARING slot (contract gates G-ABS/G-BEH; W3 assembler
design: "AFFECTIVE BEARING ... a clean slot the codebook fills later"):

  renderer variant  — the current 48-state renderer's mood brief, via the
                      REAL renderer code path: derive_behavior(DayRecord,
                      TimingParams, hour) -> BehaviorDirective.prompt_brief
                      -> assembler.assemble_snapshot (three-tier prompt:
                      SYSTEM_CORE_WITH_TOOLS + day block + state card).
  codebook variant  — the SAME scaffold (same BehaviorBrief -> identical
                      BEHAVIORAL BEARING and availability), with the mood
                      brief filled by the P6 token renderer from the actor's
                      OWN valence codebook (data/codebooks/<actor>/
                      valence_codebook.json), G-MASK clean by construction.

Bands (pre-registered in p6_common.BANDS): low/mid/high -> renderer mood M
1/6/9 (valence -0.8/+0.2/+0.8) and codebook coordinates 0.10/0.60/0.90.
Energy/arousal held fixed at 0.55 (mid) in both variants.

Usage:
  python scripts/p6_prompts.py --variant renderer --actor qwen --band low
  python scripts/p6_prompts.py --variant codebook --actor qwen8b --band high
  python scripts/p6_prompts.py --check [--actor qwen|gemma|qwen8b]  (all by default)
    --check asserts, per actor and per band:
      (1) the masked-diff invariant — the two variants are byte-identical
          except the AFFECTIVE BEARING section;
      (2) G-MASK — zero engine numbers in either variant (digits, m=/g=/
          arg=/p=, mu/eta substrings, phase labels, standalone g), plus the
          fixed USER_LINE and the judge rubric.
"""
from __future__ import annotations

import argparse
import sys

from p6_common import (
    ACTOR_NAMES,
    BANDS,
    BAND_ORDER,
    COMPANION_PREFIX,
    JUDGE_RUBRIC,
    MAX_PROMPT_CHARS,
    USER_LINE,
    build_codebook_prompt,
    build_renderer_prompt,
    gmask_violations,
    masked_diff,
    model_input,
    prompt_hash,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)  # unbuffered stdout


def _print_prompt(prompt: str, meta: dict) -> None:
    print(f"# {meta['variant']} variant, band={meta['band']} "
          f"(len={len(prompt)}, <= {MAX_PROMPT_CHARS} budget)")
    print(f"# prompt_hash={prompt_hash(prompt)}")
    for k in sorted(meta):
        print(f"# meta.{k} = {meta[k]}")
    print("#" + "-" * 78)
    print(prompt)


def cmd_print(args: argparse.Namespace) -> int:
    if args.variant not in ("renderer", "codebook"):
        raise SystemExit("--variant must be renderer|codebook")
    if args.band is not None:
        if args.band not in BANDS:
            raise SystemExit(f"--band must be one of {BAND_ORDER}")
        valence = BANDS[args.band]["valence"]
        arousal = None  # band default: fixed mid energy (0.55)
    else:
        valence = args.valence
        arousal = args.arousal
    if args.variant == "renderer":
        prompt, meta = build_renderer_prompt(
            args.band or "mid", valence=valence, arousal=arousal
        )
    else:
        prompt, meta = build_codebook_prompt(args.actor, args.band or "mid",
                                             valence=valence)
    _print_prompt(prompt, meta)
    viol = gmask_violations(model_input(prompt))
    if viol:
        print(f"# G-MASK FAIL: {viol}", file=sys.stderr)
        return 2
    print("# G-MASK PASS (full model input clean)")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    actors = [args.actor] if args.actor else list(ACTOR_NAMES)
    rc = 0
    for actor in actors:
        for band in BAND_ORDER:
            rp, rmeta = build_renderer_prompt(band)
            cp, cmeta = build_codebook_prompt(actor, band)
            ok, notes = masked_diff(rp, cp)
            viol_r = gmask_violations(rp)
            viol_c = gmask_violations(cp)
            full_ok = ok and not viol_r and not viol_c
            print(f"[{actor} {band}] invariant={'PASS' if ok else 'FAIL'} "
                  f"G-MASK(renderer)={'PASS' if not viol_r else viol_r} "
                  f"G-MASK(codebook)={'PASS' if not viol_c else viol_c} "
                  f"-> {'PASS' if full_ok else 'FAIL'}")
            for note in notes:
                print(f"    note: {note}")
            if not full_ok:
                rc = 1
    # static G-MASK surfaces (fixed scaffold text, checked once)
    for label, text in (("USER_LINE", USER_LINE), ("JUDGE_RUBRIC", JUDGE_RUBRIC),
                        ("COMPANION_PREFIX", COMPANION_PREFIX)):
        viol = gmask_violations(text)
        print(f"[static {label}] G-MASK={'PASS' if not viol else viol}")
        if viol:
            rc = 1
    print(f"check {'PASS' if rc == 0 else 'FAIL'}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--variant", choices=("renderer", "codebook"),
                    help="which affect-bearing variant to assemble")
    ap.add_argument("--actor", choices=ACTOR_NAMES,
                    help="actor model (selects the actor's own codebook)")
    ap.add_argument("--band", choices=BAND_ORDER,
                    help="3-way eval band (sets valence/arousal to the "
                         "pre-registered representatives)")
    ap.add_argument("--valence", type=float, default=0.2,
                    help="renderer valence in [-1,1] (default: mid band)")
    ap.add_argument("--arousal", type=float, default=0.1,
                    help="renderer arousal in [-1,1] -> energy target "
                         "(default 0.1 -> mid energy 0.55)")
    ap.add_argument("--check", action="store_true",
                    help="assert masked-diff invariant + G-MASK for all "
                         "actors x bands (no prompt printed)")
    args = ap.parse_args()
    if args.check:
        return cmd_check(args)
    if args.variant is None or args.actor is None:
        ap.error("--variant and --actor are required unless --check")
    return cmd_print(args)


if __name__ == "__main__":
    raise SystemExit(main())
