# Brain Improvement Plan — Ontology / Ontologist Direction

> Tổng hợp nguồn ngoài + đối chiếu với trạng thái nội tại của brain, kèm plan
> 3 milestone. Lưu ngày: 2026-04-22.

---

## 1. Trạng thái nội tại brain (ground truth)

Lấy trực tiếp từ MCP brain (`brain_stats`, `brain_recent`, `brain_live_coverage`).

### 1.1 Số liệu

| Metric | Value | Nguồn |
|---|---|---|
| Entities | 63 | `brain_stats` |
| Facts | 283 | `brain_stats` |
| Facts/entity ratio | 4.5 | tự tính (rất thưa) |
| Live recall miss rate (14 ngày) | **83%** (5/6) | `brain_live_coverage` |
| Avg top score | 0.41 | `brain_live_coverage` |
| Items kẹt playground | 198 / 57 promoted | insight `autoresearch-metric-flat-despite-87-cycles` |
| Question Coverage Score | 0.286 (flat 87 cycles, ~3 tháng) | insight cùng tên |
| Code repo status | tooling broken, data intact | entity `projects/brain` |

### 1.2 Insight đã ghi (gốc của plan)

- `ontology-database-odb-design-with-confidence-gating` — RDF Oxigraph design, **chưa build**
- `brain-architecture-separation-of-primary-and-derivative-entities` — schema phân lớp
- `brain-recall-ranking-architecture-hybrid-bm25-semantic-via-rrf` — hybrid hiện tại
- `brain-recall-ranking-modifiers` — path penalty, density boost, recency decay
- `hybrid-search-ranking-bug-entity-branch-missing-primary-buried-by-derivative`
- `brain-pipeline-autonomy-architecture-gap` — pipeline đơn-tuyến, no self-repair
- `brain-pipeline-split-asymmetry` — notes realtime, sessions 60-180s
- `brain-reconcilepy-detects-but-does-not-auto-resolve-conflicting-entities`
- `root-causes-across-brain-layers-missing-substrate-non-atomic-writes-duplication`
- `eval-score-field-naming-ambiguity` — `score` thực ra là `miss_rate`
- `inferential-fabrication-with-false-citation-in-agent-responses`
- `hook-design-principle-extract-in-session-not-in-hook`
- `autoresearch-metric-flat-despite-87-cycles`

### 1.3 Issues đang mở

- `entity-quality-validation-mechanism-missing` — 1482 entities, không re-validate
- `brain-pipeline-does-not-ingest-project-level-files`
- `brain-mcp-flapping`
- `brain-init-preset-picker-crashes-on-non-persona-yaml-files`
- `brain-project-readme-significantly-outdated-versus-codebase`

---

## 2. Nguồn ngoài tham khảo

### 2.1 Skills cùng nhóm (có thể cài thử ngay)

| Skill | Install | Lõi ý tưởng | Source |
|---|---|---|---|
| `ontology` (ClawHub) | `npx clawhub@latest install ontology` | Typed knowledge graph, append-only `memory/ontology/graph.jsonl`, ~15 entity types, constraint validation mọi mutation | [mcp.directory/skills/ontology](https://mcp.directory/skills/ontology) · [discoveraiskills.com/skills/ontology](https://discoveraiskills.com/skills/ontology) |
| `self-improving-agent` | `clawhub install self-improving-agent` | Memory dạng text learnings, gate "3 lần trong 30 ngày" mới promote | ClawHub registry |
| `skill-vetter` | `clawhub install skill-vetter` | Security scan trước khi install skill khác | ClawHub registry |
| Anthropic `mcp-builder` (đã có local) | `~/.claude/skills/mcp-builder/` | Build MCP servers — dùng để đóng gói brain thành skill ClawHub | Local |

Stats từ ClawHub (Feb 2026): `ontology` đã có 154,493 downloads / 1,072 installs (ratio 144:1).
Theo review trên dev.to, đây là skill **duy nhất trong top 5** mà "actually does something" khi cài.

### 2.2 Open-source ontologist agent systems

| Project | Repo | Đặc trưng kiến trúc |
|---|---|---|
| **OntoGenix** | [github.com/tecnomod-um/OntoGenix](https://github.com/tecnomod-um/OntoGenix) | Multi-agent FSM: `Assyst Bot` (orchestrator) → `PromptCrafter` → `PlanSage+RAG` → `OntoBuilder` → `OntoMapper` → `KGen`. Self-repair loop. 97% first-pass success. GPL-3.0. |
| **OntoCast** | `pip install ontocast` · [growgraph.dev/open-source/ontocast](https://growgraph.dev/open-source/ontocast) | Agentic semantic triple extraction, ontology-guided, output RDF/Turtle vào Neo4j/Fuseki. Multi-format input (text, JSON, PDF, Markdown). |
| **SCHEMA-MINER Pro** | [schema-miner.readthedocs.io](https://schema-miner.readthedocs.io/) | Human-in-the-loop, ontology grounding qua lexical heuristics + semantic similarity, ground vào QUDT formal ontologies. |
| **OntoKG** | [github.com/Prorata-ai/OntoKG](https://github.com/Prorata-ai/OntoKG) | Intrinsic-relational routing: 94 modules / 8 categories. 34M nodes / 61M edges từ Wikidata Jan 2026. |
| **Wikontic** | [github.com/screemix/Wikontic](https://github.com/screemix/Wikontic) | Multi-stage pipeline, dedup, ontology-consistent KG aligned với Wikidata. |

### 2.3 Papers (2025-2026)

| Paper | Citation | Lý do quan trọng cho brain |
|---|---|---|
| **OntoEKG: LLM-Driven Ontology Construction for Enterprise KGs** | [arXiv:2602.01276](https://arxiv.org/abs/2602.01276), Oyewale & Soru, Feb 2026 | Pipeline 2 pha: **extraction** module (classes + properties) + **entailment** module (logical hierarchy → RDF). F1 0.724. Đúng pattern brain cần để tách `extract.py` khỏi `reconcile.py`. |
| **Development of Ontological KBs by Leveraging LLMs** | [arXiv:2601.10436](http://arxiv.org/abs/2601.10436v1), Le et al., Jan 2026 | Iterative methodology, continuous refinement cycles. Validation framework cho việc audit `playground/` 198 items. |
| **OntoKG: Ontology-Oriented KG with Intrinsic-Relational Routing** | [arXiv:2604.02618](https://arxiv.org/abs/2604.02618), Li et al., Apr 2026 | **Intrinsic vs relational routing**: phân loại property → node attribute (intrinsic) hoặc edge (relational). Lời giải trực tiếp cho việc graph brain quá thưa (4.5 facts/entity). |
| **Wikontic: Wikidata-Aligned Ontology-Aware KGs với LLMs** | [EACL 2026, aclanthology.org/2026.eacl-long.388](http://www.aclanthology.org/2026.eacl-long.388/), Chepurova et al. | Multi-stage dedup pipeline. Pattern dedup mạnh hơn `string similarity ≥ 0.85` của brain. |
| **Extract-Define-Canonicalize (EDC)** | Zhang & Soh (được OntoKG cite) | 3 bước extract → define types → canonicalize. Direct mapping với pipeline brain hiện tại. |
| **LLMs4SchemaDiscovery** | ESWC 2025, Sadruddin et al. | HITL workflow cho scientific schema mining — nguồn gốc Schema-Miner Pro. |

### 2.4 Anthropic Agent Skills (canonical reference)

- [Claude Agent Skills overview](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview)
- Skill structure: `SKILL.md` với YAML frontmatter, progressive disclosure 3 levels
  (metadata always loaded, instructions on-trigger, resources on-reference).
- Trust model: chỉ dùng skill tự viết hoặc từ Anthropic. ClawHub Feb 2026 audit:
  341/2,857 skills bị flag malicious → **bắt buộc đọc skill code trước khi install**.

---

## 3. Đối chiếu brain ↔ ontology skill

| Mục | ontology skill (ClawHub) | brain (hiện tại) |
|---|---|---|
| Storage | `memory/ontology/graph.jsonl` (append-only JSONL → migrate SQLite) | `~/.brain/.brain.db` + `entities/*.md` + `.vec/` |
| Entity types | Person, Organization, Project, Task, Event, Document, Message, Account, Device, Action, Policy (~15) | people, projects, decisions, insights, issues, domains, locations, techniques (8) |
| Trigger | "remember…", "what do I know about X?", "link X to Y" | brain_recall / brain_get / hooks tự harvest |
| Query | scripts hoặc file ops trực tiếp | MCP tools (BM25 + semantic + graph SPARQL) |
| Validation | Constraint system, mọi mutation phải pass schema | Reconcile pipeline + audit gate (2-ref + high-conf), **không validate type/relation** |
| Mutation log | Append-only JSONL operations | Git history (gián tiếp) |
| Distribution | ClawHub registry, 154k downloads | Self-hosted, không có registry |

**Kết luận**: brain mạnh hơn về *retrieval* (hybrid BM25+semantic+graph), yếu hơn về
*write-side validation* và *mutation auditability*. 3 thứ đáng steal:
1. Append-only JSONL ops log
2. Constraint validation mọi mutation
3. Type system rộng hơn (Action, Policy, Message, Account, Device)

---

## 4. Plan 3 milestone

### M0 — Stop the bleeding (1–2 ngày, ROI cao nhất)

| # | Task | Lý do | Acceptance |
|---|---|---|---|
| 0.1 | Fix `Eval_Score` field name: `score → miss_rate`, thêm `coverage = 1 - miss_rate` | Đang đọc ngược dấu metric, mọi quyết định downstream sai (insight `eval-score-field-naming-ambiguity`) | Dashboard hiển thị đúng coverage |
| 0.2 | Fill `who-i-am.md`: location, timezone, food preferences, family (Thuha, Trinh) | 4/8 miss query gần nhất là identity/location | Live coverage 7d ≥ 50% |
| 0.3 | Fix entity branch trong hybrid search: primary entity boost ×1.5, derivative ×1.0 | Insight `hybrid-search-ranking-bug-entity-branch-missing-primary-buried-by-derivative`. "brain là gì" miss với best_score 0.51 | "brain là gì" → primary `entities/projects/brain.md` ở rank #1 |
| 0.4 | Fix `brain init` preset picker crash trên `auto_clean.yaml` | Issue `brain-init-preset-picker-crashes-on-non-persona-yaml-files` | `brain init` không crash với non-persona yaml |
| 0.5 | Update README + CLAUDE.md với 4 MCP tools mới (`brain_failure_*`, `brain_graph_*`) | Issue `brain-project-readme-significantly-outdated-versus-codebase` | README diagram đầy đủ |

**Acceptance tổng**: live coverage ≥ 50% trên 6 query gốc.

### M1 — Ontologist agent loop (1 tuần)

Lấy pattern từ **OntoGenix** (multi-agent FSM) + **OntoEKG** (2-pha):

```
harvest → [PlanSage] → [OntoBuilder] → [Reconciler] → [Validator] → entities/
              ↑                                            ↓
              └──────────── failure ledger ◀───────────────┘
```

| Agent | Vai trò | Substrate |
|---|---|---|
| **PlanSage** | Đọc `playground/` 198 items kẹt, đề xuất schema mới (type mới, relation mới, property mới). Output: PR vào `schema.yaml` | Mới: `brain/agents/plansage.py` |
| **OntoBuilder** | Extract entity từ session, gán type theo schema hiện tại | Đã có `brain/extract.py` — chỉ cần tách rõ trách nhiệm |
| **Reconciler** | Auto-resolve 3 patterns đã detect: contested facts (vote by recency + source count), single-source decay (TTL 30d → soft-delete), duplicates (sim ≥ 0.85 → auto-merge) | `brain/reconcile.py` có sẵn detection, thiếu resolution |
| **Validator** | Trước khi promote: (a) intrinsic vs relational routing kiểu OntoKG, (b) constraint check kiểu ClawHub `ontology` (append-only + schema), (c) **citation chain check** để bắt fabrication | Mới |

**Promotion gate mới** (thay 2-ref + high-conf):
- 1 ref nếu source = direct user statement + recency < 7d
- 2 ref nếu indirect/inferred
- Auto-decay sau 30d nếu single-source và không được re-cited

**Failure ledger**: đã có sẵn (`brain_failure_record/list`) — wire vào: mỗi miss
`brain_recall` với `best_score < threshold` ghi 1 row, PlanSage đọc weekly để
đề xuất schema mở rộng.

**Acceptance**: 198 → < 50 items kẹt playground, fabrication rate đo được.

### M2 — Ontology Database (ODB) build-out (1–2 tuần)

Build cái design đã viết sẵn (`ontology-database-odb-design-with-confidence-gating`):

1. **Storage**: Oxigraph (đã chọn) RDF triple store song song với markdown.
   Mỗi fact promoted → 1 triple `<subject> <predicate> <object>` + named graph
   chứa `confidence, source, date`.

2. **Intrinsic vs relational routing** (từ OntoKG arXiv:2604.02618):
   mỗi property gắn 1 trong 2 nhãn:
   - `intrinsic` → node attribute (vd: `Person.name`, `Project.status`)
   - `relational` → edge (vd: `Person worksAt Org`, `Project hasOwner Person`)

   Hiện brain có `brain_graph_query` SPARQL nhưng chỉ ~283 facts cho 63 entities
   → 4.5 facts/entity quá thưa. Tách 2 loại property sẽ kéo lên 15+.

3. **Append-only JSONL mirror** (kiểu ClawHub `ontology`): `~/.brain/ops.jsonl`
   ghi mọi mutation `{op, entity, ts, agent}`. Cho phép replay + audit trail
   thực sự, không phụ thuộc git history.

4. **Citation contract**: response cần cite phải đính kèm
   `{source_path, line_range, confidence}`. Validator block response nào claim
   fact ngoài subgraph đã cite → kill class lỗi
   `inferential-fabrication-with-false-citation-in-agent-responses`.

**Acceptance**: graph queries có nội dung thực, 4.5 → 15+ facts/entity,
fabrication rate → 0 trên test set.

### M3 (optional) — Đóng gói + đối sánh

- **Cài thử ClawHub `ontology` skill** vào `~/.claude/skills/ontology/` chạy
  song song 1 tuần với brain. So sánh 2 graph: cái nào catch fact chính xác hơn
  cho cùng session log → quyết định steal pattern nào.
- **Đóng gói brain MCP thành ClawHub skill** `brain-memory` để publish — bắt
  đầu từ `mcp-builder` skill có sẵn ở `~/.claude/skills/mcp-builder/`.

---

## 5. Bảng ưu tiên

| Ưu tiên | Khi nào làm | Effort | Kỳ vọng |
|---|---|---|---|
| **M0.1 + M0.2** | ngay hôm nay | 30 phút | Coverage 17% → 50%+ |
| **M0.3 + M0.4 + M0.5** | tuần này | 1 ngày | Hết bugs cản trở onboarding |
| **M1 Reconciler auto-resolve** | tuần này | 2 ngày | 198 → <50 items kẹt |
| **M1 Validator + citation contract** | tuần này | 2 ngày | Fabrication rate đo được, không còn class lỗi |
| **M2 ODB intrinsic/relational** | tuần sau | 1 tuần | Graph queries có nội dung thực |
| **M3 đóng gói ClawHub skill** | optional | 2 ngày | Brain publish được |

---

## 6. Mapping plan ↔ source

| Plan item | Lấy ý tưởng từ |
|---|---|
| M0.1 fix metric naming | Insight nội bộ `eval-score-field-naming-ambiguity` |
| M0.3 entity branch boost | Insight nội bộ `hybrid-search-ranking-bug-...` |
| M1 PlanSage agent | OntoGenix `PlanSage+RAG` agent |
| M1 4-agent FSM | OntoGenix multi-agent architecture |
| M1 2-pha pipeline | OntoEKG (arXiv:2602.01276) extraction + entailment |
| M1 promotion gate revision | LLMs4SchemaDiscovery HITL workflow + ClawHub `ontology` constraint |
| M1 failure ledger loop | Insight nội bộ `root-causes-across-brain-layers-...` |
| M2 RDF Oxigraph store | Insight nội bộ `ontology-database-odb-design-...` |
| M2 intrinsic/relational routing | OntoKG (arXiv:2604.02618) |
| M2 append-only ops.jsonl | ClawHub `ontology` skill storage pattern |
| M2 citation contract | Insight nội bộ `inferential-fabrication-...` + Wikontic dedup |
| M3 đóng gói skill | Anthropic Agent Skills docs + ClawHub publishing flow |

---

## 7. Câu hỏi mở cần quyết định

1. **Schema source-of-truth**: `schema.yaml` (declarative) hay `schema.py` (code)?
   OntoGenix dùng prompt-based, brain hiện hardcode trong `extract.py`.
2. **Có nên adopt full 15 entity types của ClawHub** hay giữ 8 hiện tại + thêm
   `Action` & `Policy` (đủ cho audit trail + decay rule)?
3. **PlanSage chạy on-demand hay weekly cron**? OntoGenix on-demand qua FSM,
   nhưng brain đã có launchd có sẵn → weekly chi phí thấp hơn.
4. **Fabrication test set**: tự tay tạo 20 cặp (session, ground-truth fact) hay
   replay từ corrections.md đã có?
