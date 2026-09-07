# aichat_xterm — environment & test notes (2026-09-04)

Standalone app: the brain-stormer / 3-lane terminal UI at `aichat_xterm.py`.
Does NOT import niceai. Slim by design — NiceGUI frontend + a `aichat` CLI
subprocess doing the actual model work, plus `viking.py` for optional memory.

---

## 0. Project files (all in AI_ENGINE root unless noted)

| File                                   | Role                                                                                    |
| -------------------------------------- | --------------------------------------------------------------------------------------- |
| `aichat_xterm.py`                      | THE app — NiceGUI UI, hat detection, prompt assembly, launches `aichat`                 |
| `project_controller.py`                | niceai's original — NOT shipped, NOT imported by the panel (kept for niceai)     |
| `aichat_project_controller.py`       | panel's COPY (2026-09-04): listing, root/here index, `.nav.md`, doc attach. RULE: nav fixes land in BOTH files |
| `aichat_model_controller.py`         | Control Center ENGINE: Ollama ps/load/evict, GGUF scan/import/delete-traces, tuning, HF search/download, opencode-auth import (no NiceGUI, no niceai imports) |
| `auth_store.py`                      | gpg vault (existing module, reused as-is for the Vault popup)                            |
| `viking.py`                            | OpenViking plugin: `vput` / `vctx` / `vfind` (lazy, never raises)                       |
| `~/.config/aichat/config.yaml`         | aichat config: active model + ollama client                                             |
| `~/.config/aichat/roles/brainstorm.md` | entry lane role (holds the "hats" behaviour)                                            |
| `~/.config/aichat/roles/plan.md`       | planner role                                                                            |
| `~/.config/aichat/roles/exec.md`       | executor role (code)                                                                    |
| `~/.config/aichat/roles/qc.md`         | reviewer role                                                                           |
| `~/.openviking/ov.conf`                | OpenViking config (embedding model, workspace)                                          |
| `openviking.service`                   | systemd unit for the Viking server                                                      |

Import chain: `aichat_xterm.py` → `aichat_project_controller` → (lazy) `viking`.
No other local modules are imported; `viking` is only reached via
`aichat_project_controller` on first use, so boot cost stays near zero.

---

## 1. Runtime / versions (verified on this box)

| Component         | Version / value                               |
| ----------------- | --------------------------------------------- |
| OS                | Debian (i5-6500T, 4C/4T, ~15GB RAM, CPU-only) |
| Python            | 3.11.14                                       |
| venv              | `venv_ui/` (the app runs from it)             |
| NiceGUI           | 3.14.0                                        |
| aichat CLI        | 0.30.0 (Rust binary)                          |
| OpenViking        | openviking 0.4.16 + openviking-sdk 0.1.8      |
| FastAPI / uvicorn | 0.141.1 / 0.52.1                              |

## 2. Python dependencies (all inside venv_ui)

```
pip install nicegui==3.14.0
pip install openviking==0.4.16 openviking-sdk==0.1.8
pip install requests fastapi uvicorn          # openviking pulls these too
```

aichat_xterm.py itself only imports: `nicegui`, `aichat_project_controller`,
`viking` (lazy), stdlib. `aichat_project_controller.py` is the panel's copy in the
same folder (task-order parsing + `build_project_context` for listing).

## 3. aichat CLI (the actual LLM client)

- Binary: `aichat` (on PATH), version 0.30.0
- Config: `~/.config/aichat/config.yaml`
  - `model: ollama:qwen2.5-3b`  ← ACTIVE model (plain 1.9GB `qwen2.5-3b:latest`)
  - backend: openai-compatible client `ollama` at `http://localhost:11434/v1`
- Roles dir: `~/.config/aichat/roles/` — 4 files:
  - `brainstorm.md` (the entry lane — has the "hats": explore/clarify/explain/structure/summarize)
  - `plan.md`, `exec.md`, `qc.md`
- RAG / agents / macros / functions: **all EMPTY, none configured** (deferred to docs project)

## 4. Ollama (models present)

```
qwen2.5-3b-q4km:latest          4.7 GB   (tested variant)
qwen3-embedding:0.6b            639 MB   (Viking embeddings — REQUIRED for vfind/vput)
qwen2.5-3b-instruct-q6:latest   2.8 GB
qwen25-3b-q4km:latest           1.9 GB
qwen2.5-3b:latest               1.9 GB   ← aichat config currently points here
phi3-fixed:latest               2.4 GB
gemma4:e4b                      9.6 GB
qwen3:4b                        2.5 GB
```

- To switch the UI's model: change `~/.config/aichat/config.yaml` line 1
  `model:` + add the name under `clients[].models[].name`.
- The embedding model `qwen3-embedding:0.6b` must stay pulled, or Viking
  `vfind`/`vput` silently degrade (this was the Aug/early-Sep breakage).

## 5. OpenViking (optional memory — only for `<vfind>`/`<vctx>`/recall keywords)

- Server: `openviking-server` (installed in `venv_ui/bin/openviking-server`)
- Config: `~/.openviking/ov.conf`
  - workspace: `/home/joao/.openviking/data`
  - embedding.dense: ollama `qwen3-embedding:0.6b` @ `http://localhost:11434/v1`, dim 1024
- Port: `127.0.0.1:1933` (`viking.py` `SERVER_URL`, env `OPENVIKING_URL`)
- systemd unit: `openviking.service` (AI_ENGINE root → `/etc/systemd/system/`)
- The app works WITHOUT OpenViking; it just loses semantic memory recall.

## 6. Run / test checklist

1. `ollama list` — confirm `qwen2.5-3b:*` + `qwen3-embedding:0.6b` present.
2. `systemctl status openviking` — optional but healthy when used.
3. (Re)start the app: `venv_ui/bin/python aichat_xterm.py` (serves :8080).
4. Smoke test a prompt; watch the top `· BRAINSTORM / <HAT>` badge change
   color per intent (EXPLORE sky / EXPLAIN green / CLARIFY amber /
   STRUCTURE purple / SUMMARIZE pink).

## 7. Notes for me/it codermaster (future session)

- `build_project_context(project, workspace_root)` in `aichat_project_controller.py`
  is the single source of truth for project listing — shared by niceai and
  aichat_xterm. Do NOT duplicate it.
- Hats: `detect_hat()` + `_summarize_target()` + `_explain_depth()` in
  aichat_xterm.py. New intents = add a keyword tuple, not a classifier.
- Two parked todos: (a) EXPLAIN depth mirror DONE; (b) `crew`/`author` full-
  text pipeline — deferred to 9–16B hardware.

---

## 8. Supported interaction switches (what to say, what it does)

The brainstormer auto-detects your intent from a few keywords and switches
"hats" (state) each turn WITHOUT changing role. The top badge `· B / HAT`
shows the active hat, color-coded.

| You say                                                              | Hat                  | Behaviour                                                                              |
| -------------------------------------------------------------------- | -------------------- | -------------------------------------------------------------------------------------- |
| (anything casual, new topic)                                         | EXPLORE              | idea-level, defines direction, stays terse                                             |
| `explain X` · `expand` · `tell me more`                              | EXPLAIN              | explains directly, 2–5 subtopics                                                       |
| `expand more` · `even more` · `go deeper` · `break it down`          | EXPLAIN (STRUCTURED) | ordered breakdown, subsections — planner-level                                         |
| `articulate better` · `technical detail` · `in depth` · `exhaustive` | EXPLAIN (TECHNICAL)  | concrete precise detail — exec-level                                                   |
| `what do you mean` · `clarify` · `i don't understand`                | CLARIFY              | restates/explains the confusing part                                                   |
| `break into steps` · `structure it` · `outline`                      | STRUCTURE            | ordered steps/outline                                                                  |
| `summarize` · `shorten` · `tl;dr` · `in short`                       | SUMMARIZE            | make it shorter                                                                        |
| `even shorter` · `very few words` · `one sentence` · `diminutive`    | SUMMARIZE (TIGHT)    | one sentence / ≤3 bullets                                                              |
| `number 2`                                                           | —                    | most recent item #2 in conversation                                                    |
| `number 2 xerox`                                                     | —                    | finds item #2 mentioning "xerox", even earlier; says "not found" rather than inventing |

### Filesystem lookup vs conversation recall (IMPORTANT)

- **"do you see / is there / called / named / what files / list the projects"** → FILESYSTEM lookup into PROJECT STRUCTURE data. Answer from that block only.
- **"number 2" / "item 2" / "#2"** → CONVERSATION recall into the session transcript.
- These never mix. A "do you see a folder called X" must NOT be answered as "item N".
- **Task-status lookups** ("how many unchecked", "outstanding", "left to do",
  "status", "task.md") also inject project context → the pre-counted
  "Task order: N done, M outstanding" line, so the count comes from
  `parse_task_order`, not the model hand-counting a raw file.

### Listing conditions (name / date / age)

File lines carry `name (size, age (date))`, e.g. `index.html (53KB, 2d ago (2026-09-01))`.

- name conditions: `named` / `called` / `starts with` / `starts by` / `ends with` / `contains` / `has the number` / `extension` / `matches`
- date/age conditions: `younger than` / `older than` / `newer than` / `created after·before` / `modified after·before` / `last modified` / `since` / `in the last N days`
- NOTE: "age" is **modification time** (`mtime`) — Linux has no portable true
  creation time. `Nd ago (date)` is the practical "how recent" signal.

Role handoffs (explicit):

- want a committed ordered plan → say `switch to .role plan` (or click PLAN)
- want actual code → `switch to .role exec` (or click EXEC)
- review output → `switch to .role qc`

## 9. Role samples (one or two each)

**brainstorm** — "explain PTSD in summary"

> PTSD is a psychiatric condition arising after experiencing/witnessing trauma… *(then 2–5 short conceptual subtopics)*

**brainstorm (summarize)** — "summarize this in very few words"

> *(one compressed sentence, smaller than the input)*

**plan** — "plan a landing page"

> 1. HTML structure — depends on: nothing
> 2. Styling — depends on: 1
>    …ready for .role exec

**exec** — "write a python script to sum a list"

> ```python
> def total(xs):
>     return sum(xs)
> ```

**qc** — "review this script"

> PASS — or a short bullet list of issues only (no fixes, no praise).

#### Results w Prompt: describe double slit experiment in quantum physics in short summary,using **

**phi3 (2,4gb) w Harmony AI **| 60 words, 410 chrs |  without 73 words, 536 chrs***

**gemma4:e4b b (9 gb) w Harmony AI |  25 words , 193 chrs | without 243 words, 1662 chrs**

---

## 10. Navigation upgrade (2026-09-04) + share-with-friends zip

### Root index (always on, ungated)

Every prompt carries a ~8-line ROOT INDEX with stable `[tags]` — the model
always knows where things live regardless of phrasing:

```
[ROOT] <appdir> = app home
[PROJECTS] WORKSPACE/ = main project folders (N): `a`, `b`, ...
[RECORDS] History/ = niceai session records + install scripts
[TEMPLATES] WORKSPACE/_templates/ = starter templates
[BACKUPS] backup/ = code backups (.bak files)
```

Tag meanings live in `ROOT_TAGS` (`aichat_project_controller.py`) — edit there (and mirror to `project_controller.py`).
Why ungated: orientation questions ("where are the project folders") miss
every trigger list, so the gated project block is empty for them — the root
index still lands. Why not aichat native `-f <dir>`: `-f` recursively DUMPS
full contents (verified, incl. PDF text) = token blowup; deterministic index
here, native `-f` only for single doc files.

### `.nav.md` per-folder index (preferred, live fallback)

- If `<proj>/.nav.md` exists → its file list is used as-is.
- Else live `ls -la` equivalent (`os.listdir` + `os.stat`, capped 20).
- Either way the model never counts/respells: totals pre-computed
  (`Files (15 total)`), every name in ``backticks`` one-per-line.
- Task dump trimmed: pending/parked in full, done collapses to count + last 3
  (a 22-done project no longer bloats every prompt — this was the slow query).
- `write_nav_file(proj_dir, purpose)` (re)generates; all 9 WORKSPACE folders
  seeded. Builder should call it on new-folder creation (TODO).
- `tmp` folder ≠ `#tmp` parked marker (role rule 6); `[parked]` tasks are
  neither outstanding nor done.

### Doc attach (RFP path)

`find_doc_attach(prompt, base_dir)` → named doc files get native `-f`:
single files only, 1MB cap, max 3, project-scoped (a named WORKSPACE folder
sorts first). Triggers: extract/read/summar/find/locate/poc/point of contact/
rfp/opportunity/attach. Native `-f` reads: pdf, md/txt/json/csv/py/html/js/
css/xml/yaml/log/sh. Office binaries convert first via stdlib
`office_to_text` → `/tmp/aichat_docs/<name>.txt` sidecar with `SOURCE FILE:`
header: `.docx/.xlsx/.pptx` yes; old `.doc/.xls/.ppt` excluded (binary OLE +
"doc" collides with conversational "document").

### UI (needs panel restart to load)

- Role buttons: brainstorm/plan 🌈 rainbow, exec red, QC green (`!important`
  backgrounds — Quasar bg classes would otherwise win).
- Chat label: `AI Harmony <B|P|E|QC>` in the lane color, initial moved right
  of Harmony; user requests render light blue.
- Orange ↻ header button = in-place restart (`os.execv`, no systemd unit).
- Shift+B toggles the file tree.

### Friend zip (`share_panel.sh` → `harmony-panel-<date>.zip`)

Contains: `aichat_xterm.py`, `aichat_project_controller.py`, `viking.py`,
`aichat_model_controller.py`, `auth_store.py`,
`aichat_xterm_READ_ME.md`, `run_panel.sh`, `aichat-config/` (config.yaml +
roles/*.md — NEVER messages.md session logs), `WORKSPACE/neonrunner/`
slimmed (`.backups/` excluded, `.nav.md` kept) + `INSTALL.txt`.
Friend needs: python3 + `pip install nicegui`, `aichat` binary, Ollama + any
model (edit model names in `aichat-config/config.yaml`, copy it to
`~/.config/aichat/`). BASE_DIR is portable (`Path(__file__).parent`).

### Control Center (bottom ⚙️ button, right of Execute)

Thin UI in `aichat_xterm.py`, engine in `aichat_model_controller.py`
(NiceGUI-free; long ops run in daemon threads). Four popups:

- **Sys & Cld Selection**: Refresh models, Load & Launch (top notify names
  the loaded model), Unload, Tune (num_ctx/thread/temp → model_tuning.json,
  applies next load), Delete all traces (confirm gate). WHITE (unwired):
  Team Check, Fastest Cloud.
- **Model Dwnl**: HF org+search → repo → GGUF file → download + auto-import
  to Ollama + delete-GGUF checkbox.
- **Diagnostics**: Ollama reachability, imported/in-RAM lists, GGUF count,
  disk free (read-only).
- **Vault**: unlock/create/lock, add/delete service, test email,
  ⬇ Import from opencode (deepseek+openrouter only) — via `auth_store.py`
  reused as-is. `credentials.enc` NEVER ships in the friend zip.
