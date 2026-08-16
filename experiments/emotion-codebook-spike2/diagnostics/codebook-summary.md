# P4 lexical codebooks — build summary (diagnostic; gates are P5's)

Built: scripts/build_codebook.py, CPU-only, no model loading, no fresh sampling (piecewise-linear blend over the 10 measured bins per axis; RE-DERIVED probabilities; provenance = measured top-30 lists only).

| model | axis | grid nodes | candidates/node (min-max) | empty nodes | provenance violations | measured n | JS max (0.2-spacing / full grid) | valence monotonic trend | top-5 @ 0.1 | top-5 @ 0.5 | top-5 @ 0.9 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen | valence | 101 | 37.2 (30-49) | 0 | 0 | 2000 | 0.5976 / 0.021146 | rho(pos-neg)=nan; rho(share)=nan | :(0.417) 車(0.043) STRACT(0.036) 's(0.007) yz(0.006) |  =(0.081) ,(0.054)  of(0.042)  and(0.032)  is(0.029) |  I(0.112) .(0.050)  The(0.043) ,(0.035)  But(0.033) |
| qwen | arousal | 101 | 38.0 (30-55) | 0 | 0 | 2000 | 0.5837 / 0.037465 | n/a | :(0.254) 車(0.031) STRACT(0.023) 's(0.007) ’s(0.006) |  =(0.088) ,(0.047)  is(0.030)  and(0.028)  of(0.023) |  I(0.081)  The(0.079)  He(0.035)  This(0.031)  We(0.027) |
| gemma | valence | 101 | 37.1 (30-47) | 0 | 0 | 2000 | 0.3014 / 0.019794 | rho(pos-neg)=nan; rho(share)=nan | ,(0.085) .(0.072)  is(0.029)  and(0.028) -(0.025) | .(0.071) ,(0.041)  I(0.036) _(0.026) /(0.022) |  main(0.074)  for(0.047)  the(0.045) ed(0.043) ,(0.042) |
| gemma | arousal | 101 | 37.8 (30-49) | 0 | 0 | 2000 | 0.3635 / 0.021930 | n/a | .(0.091) /(0.090) _(0.058)  =(0.044) -(0.043) | .(0.078) ,(0.065) _(0.040) -(0.039) /(0.036) |  He(0.067) !(0.060) ,(0.042) .(0.034)  you(0.028) |
| qwen8b | valence | 101 | 40.5 (30-49) | 0 | 0 | 2000 | 0.4039 / 0.022074 | rho(pos-neg)=0.868; rho(share)=nan | 的(0.073) ?(0.019) ab(0.013) ly(0.011) destroy(0.010) |  I(0.031) .(0.030)  The(0.025) 的(0.022)  So(0.018) |  !(0.093)  I(0.068) great(0.044) .(0.040)  Thank(0.018) |
| qwen8b | arousal | 101 | 43.7 (30-59) | 0 | 0 | 2000 | 0.5418 / 0.057651 | n/a | ，(0.025) 0(0.021) 的(0.018) 1(0.016) .(0.013) | 的(0.042) foundation(0.018) numer(0.016) percentage(0.013) sector(0.009) | 的(0.059) thr(0.014) !(0.013) ?(0.012)  and(0.012) |
