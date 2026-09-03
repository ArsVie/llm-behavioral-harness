"""Scratch CPU pre-check of the Gemma harness hook/leaf/analytic-check path.

Replicates GemmaHarness.__init__/forward_states/backward_directions on CPU
(no CUDA) so the GPU bringup window is spent only on memory measurement.
Deletes nothing; not part of the committed artifact set.
"""
import json
import sys
import time
from pathlib import Path

import torch

SPIKE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SPIKE_ROOT))

from scripts.p2_gemma_extract import MODEL_ID, MODEL_REVISION  # noqa: E402

from transformers import AutoTokenizer, Gemma3ForCausalLM  # noqa: E402

tok = AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
assert type(tok).__name__.startswith("Gemma"), type(tok).__name__
print("tokenizer:", type(tok).__name__, "bos:", tok.bos_token_id, "eos:", tok.eos_token_id)

t0 = time.perf_counter()
model = Gemma3ForCausalLM.from_pretrained(MODEL_ID, revision=MODEL_REVISION,
                                          dtype=torch.bfloat16, low_cpu_mem_usage=True)
model.requires_grad_(False)
model.eval()
print(f"load {time.perf_counter()-t0:.1f}s")

# backbone resolution (same logic as GemmaHarness._resolve_backbone)
m = None
for cand in (model.model, getattr(model.model, "text_model", None)):
    if cand is not None and hasattr(cand, "layers") and hasattr(cand, "embed_tokens") and hasattr(cand, "norm"):
        m = cand
        break
assert m is not None, "backbone not found"
n_layers = len(m.layers)
hidden = int(getattr(model.config, "hidden_size", None) or model.config.text_config.hidden_size)
vocab = int(getattr(model.config, "vocab_size", None) or model.config.text_config.vocab_size)
tied = model.lm_head.weight.data_ptr() == m.embed_tokens.weight.data_ptr()
print(f"layers={n_layers} hidden={hidden} vocab={vocab} lm_tied={tied} "
      f"config.tie_word_embeddings={model.config.tie_word_embeddings}")

_x0 = None
fwd_acts, bwd_grads = {}, {}
collect_fwd = collect_bwd = False
pos_mask = None


def tensor_of(out):
    return out[0] if isinstance(out, (tuple, list)) else out


def mk_fwd(key):
    def hook(mod, inp, out):
        if collect_fwd:
            fwd_acts[key] = tensor_of(out)[0][pos_mask].float().mean(dim=0).detach().cpu()
    return hook


def mk_bwd(key):
    def hook(mod, gin, gout):
        if collect_bwd:
            bwd_grads[key] = gout[0][0][pos_mask].float().mean(dim=0).detach().cpu()
    return hook


m.embed_tokens.register_forward_hook(lambda mod, i, o: _x0)
for i, layer in enumerate(m.layers):
    layer.register_forward_hook(mk_fwd(str(i)))
    layer.register_full_backward_hook(mk_bwd(str(i)))
m.norm.register_forward_hook(mk_fwd("norm"))
m.norm.register_full_backward_hook(mk_bwd("norm"))

for text in ("The quiet warmth of a slow evening settles over the small room.",
             "hurt joy", "hurt", "joy"):
    ids = tok(text, add_special_tokens=False).input_ids
    ids = [tok.bos_token_id] + ids
    L = len(ids)
    input_ids = torch.tensor([ids], dtype=torch.long)
    attn = torch.ones((1, L), dtype=torch.long)
    # forward states
    collect_fwd = True
    pos_mask = torch.arange(1, L)
    with torch.no_grad():
        out = model(input_ids=input_ids, attention_mask=attn, use_cache=False)
    collect_fwd = False
    n_fwd = len(fwd_acts)
    # backward directions
    pos_mask = torch.arange(0, L - 1)
    _x0 = m.embed_tokens(input_ids).detach().requires_grad_(True)
    collect_bwd = True
    bwd_grads = {}
    out = model(input_ids=input_ids, attention_mask=attn, use_cache=False)
    logits = out.logits
    labels = input_ids[:, 1:]
    loss = torch.nn.functional.cross_entropy(
        logits[:, :-1].reshape(-1, vocab), labels.reshape(-1))
    loss.backward()
    collect_bwd = False
    z = logits[0, :-1].float()
    sm = torch.softmax(z, dim=-1)
    onehot = torch.zeros_like(sm)
    onehot.scatter_(1, labels[0].unsqueeze(1), 1.0)
    ana = ((sm - onehot) @ model.lm_head.weight.float()).mean(dim=0) / labels[0].numel()
    auto = bwd_grads["norm"].to(ana.device)
    cos = float(((ana * auto).sum() / (ana.norm() * auto.norm() + 1e-12)).detach())
    print(f"'{text[:28]}' L={L} content={L-1} fwd_layers={n_fwd} bwd_layers={len(bwd_grads)} "
          f"cos={cos:.6f}")
    assert len(bwd_grads) == n_layers + 1
    assert cos > 0.99
    _x0 = None

print(json.dumps({"cpu_precheck": "PASS", "layers": n_layers, "hidden": hidden,
                  "vocab": vocab, "lm_tied": tied}))
