---
marp: true
theme: default
paginate: true
---

# Brain — Persistent Memory for Claude Code

A second brain that never sleeps, never forgets, and gets smarter every day.

*Presented by [Your Name]*

---

## 1. What I'm Working On

**Brain** — a persistent memory layer for Claude Code.

- **Problem**: every Claude session starts from zero. Same context re-explained, decisions forgotten, nuance lost.
- **Solution**: between sessions, auto-extract entities (people, projects, decisions, insights) from transcripts and store them as a markdown vault in `~/.brain/`. Claude reads it back at session start.
- **Outcome**: open the laptop and Claude already knows who I am, what I'm building, every open task, every prior decision.

> Obsidian is the IDE. The LLM is the programmer. The wiki is the codebase.

---

## 2. What Tools I Use

| Layer            | Tool                                       |
| ---------------- | ------------------------------------------ |
| Extraction       | **Claude Haiku** (~$0.001 / session)       |
| Interactive work | **Claude Opus** via Claude Code CLI        |
| Pipeline         | **Python** — `harvest → extract → apply`   |
| Storage          | **Markdown + Git** (`~/.brain/` as a vault)|
| Viewer / editor  | **Obsidian** (wiki-links, graph view)      |
| Automation       | **Claude Code `SessionStart` hook**        |

Pipeline: `harvest_session.py` → `auto_extract.py` → `apply_extraction.py` (single source of truth, commits to git).

---

## 3. What Results I Get

- **Zero-prompt memory** — Claude remembers names, decisions, preferences across every session.
- **Compounding knowledge** — each session ingests + cross-references prior entities; the brain gets richer with use.
- **Cheap** — ~$0.001 per session for Haiku extraction; Opus only for interactive work.
- **Human-readable & portable** — pure markdown, browsable in Obsidian, diffable in Git, no proprietary DB.
- **Tested** — 30 tests covering entity CRUD, harvest, extraction parsing, reconciliation, end-to-end ingest.
- **Hands-off** — `SessionStart` hook does everything; no slash commands, no manual prompts.

> Build this once. Use it forever. Gets better every single day.
