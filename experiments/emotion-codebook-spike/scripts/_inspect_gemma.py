"""Scratch inspection: google/gemma-3-1b-pt config + tokenizer (no weights).

Reads the pinned revision's config/tokenizer via the local HF cache token.
Prints: identity, arch numbers, tie/tokenizer facts, tokenization of the
bringup probe strings. Does NOT download model weights.
"""
import json
from pathlib import Path

import huggingface_hub as hf

REV = "fcf18a2a879aab110ca39f8bffbccd5d49d8eb29"
REPO = "google/gemma-3-1b-pt"

who = hf.whoami()
print("hf whoami:", who.get("name"), "| type:", who.get("type"))

info = hf.HfApi().model_info(REPO, revision=REV)
print("sha:", info.sha)
files = sorted(s.rfilename for s in info.siblings)
print("files:", files)

snap = hf.snapshot_download(
    REPO, revision=REV,
    allow_patterns=["config.json", "generation_config.json", "tokenizer*",
                    "special_tokens_map.json", "*.model", "*.json"],
    max_workers=4,
)
print("snapshot:", snap)

cfg = json.loads((Path(snap) / "config.json").read_text())
print("model_type:", cfg.get("model_type"))
print("architectures:", cfg.get("architectures"))
print("tie_word_embeddings:", cfg.get("tie_word_embeddings"))
tc = cfg.get("text_config", cfg)
for k in ("num_hidden_layers", "hidden_size", "vocab_size", "intermediate_size",
          "num_attention_heads", "num_key_value_heads", "head_dim",
          "sliding_window", "use_bidirectional_attention", "layer_types",
          "rms_norm_eps", "rope_theta", "max_position_embeddings",
          "pad_token_id", "bos_token_id", "eos_token_id", "attention_bias"):
    if k in tc:
        print(f"  {k}: {tc[k]}")

# parameter estimate from config (embedding counted once; lm_head tied or not)
emb = tc["vocab_size"] * tc["hidden_size"]
per_layer = 0
for p in (tc["num_attention_heads"], tc["num_key_value_heads"]):
    per_layer += tc["hidden_size"] * p * tc.get("head_dim", tc["hidden_size"] // tc["num_attention_heads"])
per_layer += tc["hidden_size"] * tc["hidden_size"]  # o_proj
per_layer += 3 * tc["hidden_size"] * tc["intermediate_size"]  # gate/up/down
n_params = emb + per_layer * tc["num_hidden_layers"] + tc["hidden_size"]  # final norm (approx)
if not cfg.get("tie_word_embeddings", True):
    n_params += emb
print(f"param estimate: embed={emb/1e9:.3f}B + {tc['num_hidden_layers']}x{per_layer/1e6:.1f}M "
      f"= {n_params/1e9:.2f}B total (bf16 {n_params*2/1e9:.2f} GB)")

from transformers import AutoTokenizer  # noqa: E402
tok = AutoTokenizer.from_pretrained(snap)
print("tokenizer class:", type(tok).__name__)
print("bos:", tok.bos_token_id, repr(tok.bos_token), "| eos:", tok.eos_token_id,
      repr(tok.eos_token), "| pad:", tok.pad_token_id, repr(tok.pad_token))
print("add_prefix_space:", getattr(tok, "add_prefix_space", None))
for s in ("The quiet warmth of a slow evening settles over the small room.",
          "hurt", "joy", "love", "okay", "This is a longer sentence with several words."):
    ids = tok(s, add_special_tokens=False).input_ids
    print(f"  tok({s[:45]!r}) -> {len(ids)} ids: {ids[:12]}{'...' if len(ids) > 12 else ''}")
