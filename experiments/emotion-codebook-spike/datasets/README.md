# Datasets — provenance (P0, emotion-codebook spike)

All raw files pinned by sha256 (see `raw/`). Canonical sources only; no
substitutions. Row counts RE-DERIVED from the pinned files on 2026-08-15.

## NRC-VAD
- Source: https://saifmohammad.com/WebPages/NRC-VAD.html (Mohammad 2018, ACL)
- File: `raw/NRC-VAD-Lexicon.zip` (41,166,179 B)
- Contents: `nrc-vad/NRC-VAD-Lexicon/NRC-VAD-Lexicon.txt` — English lexicon,
  tab-separated `word V A D`, V/A/D ∈ [0,1].
- Rows: 19,974 words (header-less).
- Note: first download attempt was truncated (12.5 MB) — re-downloaded;
  archive verified with `zipfile.testzip()` (None = OK).

## Warriner et al. 2013 (ANEW-style norms, 13,915 lemmas)
- Source: https://github.com/JULIELab/XANEW — JULIELab (EmoBank authors)
  secondary distribution of the canonical Warriner, Kuperman & Brysbaert
  (2013) ratings (http://crr.ugent.be/archives/1003).
- File: `raw/Ratings_Warriner_et_al.csv` — header + 13,915 rows; columns
  Word, V.Mean.Sum, A.Mean.Sum, D.Mean.Sum, + SD/counts and demographics.
- First mirror tried (`kabartolo/valence-arousal-dataset`) 404'd; JULIELab
  distribution used instead (same canonical CSV).

## EmoBank (10k sentences, writer+reader VAD)
- Source: https://github.com/JULIELab/EmoBank (canonical repo), file
  `corpus/emobank.csv`.
- File: `raw/emobank.csv` — header + 10,062 rows; columns
  `id,split,V,A,D,text` (split: train/dev/test). V/A/D on 1–5 scale.
- Row counts per split (RE-DERIVED from pinned file): train 8,062 /
  dev 1,000 / test 1,000.

## GoEmotions (58k Reddit comments, 27 categories + neutral)
- Source: https://huggingface.co/datasets/google-research-datasets/go_emotions
  (canonical Google Research dataset; arXiv:2005.00547)
- Pinned revision: `add492243ff905527e67aeb8b80c082af02207c3` (2024-01-04).
- Files (sha256 verified against HF LFS hashes):
  - `raw/goemotions_train.parquet` (24,828,322 B; sha 5de61486…) — full
    per-rater data: 211,225 rows (58k comments × ~3.6 raters), 28 one-hot
    emotion columns + neutral.
  - `raw/goemotions_simplified_{train,validation,test}.parquet` — canonical
    58k-split version: train 43,410 / validation 5,426 / test 5,427 =
    54,263 rows total, 53,994 unique texts. RE-DERIVED counts.
- GitHub raw (goemotions.csv) 404'd on both master/main — HF Hub used.
