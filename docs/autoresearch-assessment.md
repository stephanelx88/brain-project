# Autoresearch for brain — scoping assessment

**Context**: Son asked 2026-04-23 whether Karpathy-style autoresearch (LLM propose-code-run-measure loops on a numeric fitness function) is a good fit for brain. Decision **deferred until after WS1+WS2+WS7 land** — analysis parked here for pickup later.

## Verdict

**Partial fit.** Autoresearch fits the bench-able, bounded knobs. It does NOT fit architectural, security, or identity-integrity decisions.

WS1 (merged 2026-04-23) gave brain its first real fitness function: `p@1, MRR, weak_hit_rate` on a 21-query golden set. That unlocks Tier-1 autoresearch but exposes Goodhart's-law risk on the same metric.

## Fit matrix

### GREEN — fits autoresearch loop
| Task | Search space | Notes |
|---|---|---|
| Recall hyperparam tuning — `BRAIN_RECALL_WEAK_RRF`, `NON_ASCII_SCALE`, `SEMANTIC_FALLBACK`, RRF weights, k | ~5 knobs, each small | Bayesian/grid 48-200 runs, each = 1 bench (~2 s). $0 cost. |
| Query rewriter prompt tuning | finite prompt variants | Needs held-out query set. |
| Reranker prompt tuning | finite prompt variants | Same held-out requirement. |
| Extractor prompt tuning | prompts + vault facts precision/recall | Requires held-out session set with known-good extractions. |
| Regression repair | bounded diff on single failing query | Night-shift agent; gated by "no other metric drops". |

### RED — does NOT fit autoresearch
| Task | Reason |
|---|---|
| WS6 reified `fact_claims` schema | Architectural — ontology tradeoffs are human-judgment territory. |
| WS4 scrubber rules | Security — a false negative is a secret leak. Rules need human approval. |
| WS2 MCP envelope shape | API surface — ergonomics need human stability; consumers (Claude/Cursor) depend on shape. |
| Direct writes to `entities/people/stephane.md` or `identity/*` | Identity integrity — brain's design principle is user authors their own life; auto-generated "facts about me" violate it. |
| Team coordination (Ontologist vs Architect vs Security) | Cannot auto-arbitrate tradeoffs between principled stakeholders. |

## Risk: Goodhart's law on bench-in-the-loop

Optimising `p@1` alone will produce hacks — e.g. slide the weak-match threshold until every query is "weak" (satisfies `weak_hit_rate=1.0`) while destroying `p@1`. Mitigations:

1. **Multi-metric hard gate**: accept a config change only if `p@1 + MRR + weak_hit_rate` all improve-or-tie. Any single-metric regression = reject.
2. **Held-out validation set**: use the 21-query bench to *propose* changes; keep a separate, un-seen, held-out 30+ query set to *validate* acceptance. Prevents optimiser from memorising bench queries.
3. **Token cap**: hard daily budget (`BRAIN_AUTORESEARCH_DAILY_TOK`, default 25000 Haiku ≈ $0.05/day). Loop stops when budget hits, never overruns.
4. **No direct-to-main**: all loop proposals land on `autoresearch/YYYY-MM-DD` branches, PM reviews + merges (or not) in morning.

## Proposed workstreams (post WS1+WS2+WS7)

### WS9 — `brain tune-recall` (Tier-1 autoresearch)
Grid/Bayesian search over the five recall knobs against bench. Writes best config to `~/.brain/.tune-recall.yaml`; user adopts via env import or install writes it to rc. Estimated 100 s / run / zero LLM cost. Ship as a new PR after WS7 lands so the tuned baseline reflects subject-reject + rewriter+reranker state.

### WS10 — "Night shift" agent-in-loop (Tier-2 autoresearch)
Scheduled 2 a.m. job:
1. Run bench. If any metric regressed vs prior night, alert + stop.
2. Identify the worst 3 queries (lowest per-query RRF, highest `weak_hit_rate=0` rate).
3. Ask a bounded LLM agent to propose either (a) a config tweak in the WS9 knob space, or (b) a single-file, ≤30-line code diff.
4. Run full bench + full `pytest`.
5. Only keep if multi-metric gate passes + held-out set doesn't regress.
6. Commit to `autoresearch/2026-MM-DD` branch. Post summary to `chat.md` equivalent.
7. PM merges (or reverts) in the morning.

Hard budget: $0.10 / night. Kill-switch: `touch ~/.brain/.autoresearch.disable`.

### Explicitly NOT in scope (Tier-3)
No autoresearch over schema, security rules, API surface, or identity writes. Those stay human-driven by the Ontologist / Architect / Security roles.

## Why defer

- **WS7a (subject-reject) will bump `weak_hit_rate` dramatically.** Tuning the recall knobs against today's baseline would over-fit to pre-WS7 behaviour. Wait until WS7 lands, then tune.
- **n=21 is too small** to tune against reliably. Expand the golden set to ~80-120 queries (held-out 30-40) before any Tier-1 loop.
- **No held-out set yet.** First build the held-out, THEN run any optimiser against bench. Otherwise Goodhart.

## Dependencies (for the gate-of-the-gate)
```
WS1  merged   ✅  (2026-04-23 15:47)
WS2  in progress
WS7a pending  (needs WS6 subject_slug)
Held-out set  not created yet  — prerequisite for WS9/WS10
```

## References
- Karpathy's autoresearch framing (tweet/talk 2024) — LLM loop with numeric fitness on bounded problems; evolutionary-style proposal + measurement.
- Related: DeepMind AlphaEvolve, Sakana AI-Scientist — both bound-their-search; both rely on a *cheap, deterministic* fitness function. Brain's bench meets that bar.
- Risk literature: Goodhart's law, specification gaming, reward hacking.
