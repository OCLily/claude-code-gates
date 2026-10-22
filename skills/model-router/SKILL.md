---
name: model-router
description: >
  Decide which model to use for each task or subagent. Three axes: production
  depth (Haiku/Sonnet/Opus, scaled by stakes), adjudication (Fable, the
  independent adversarial judge), and effort (low/medium/high/xhigh/max, a
  separate cost lever: session default, per-subagent frontmatter, or per
  Workflow agent() call). Invoke before dispatching any subagent, before
  planning a multi-step task, whenever a step could be solved at different
  cost/depth tradeoffs, or whenever an output needs an independent verdict.
  Prevents reflexive Opus usage, avoids under-powering high-stakes decisions,
  and keeps Fable a deliberate exception rather than a default.
  NOT for doing the task itself: only for model/tier choice; NOT a general
  agent-dispatch router. NOT for LLM API cost optimization in product code
  (cutting provider spend, prompt caching, token budgets, routing layers in
  your own app). A "which model should I use?" question belongs here only when
  it is about Claude Code subagents or steps in this harness, not about model
  choice in an application with its own API bill.
---

# Model Router — Task-Cost Decision Framework

## When to invoke this skill

- Before dispatching subagents (sets `model:` parameter)
- When planning a multi-step task (assign tiers per step)
- When a task has clearly different complexity than the default
- When an output needs an independent adversarial verdict (→ Fable)

## Three axes, not one ladder

Routing runs on **three independent axes**. Pick the axis first, then the point on it.

- **Axis 1 — Production depth:** you are *building / analyzing / authoring / deciding*. Scale by stakes → Haiku / Sonnet / Opus.
- **Axis 2 — Adjudication:** you are *judging output that already exists* (critic, contrarian, pre-mortem, independent verdict on an irreversible fork). → Fable.
- **Axis 3 — Effort:** independent of WHICH model — how hard that model tries. Session default, or overridden per subagent (frontmatter `effort:`) or per Workflow `agent()` call.

Fable is **not** a 4th depth tier stacked above Opus. It is a different job. **Opus builds; Fable judges.** Reaching for Fable on the production path is almost always wrong — go Opus. The exact Fable triggers are the three cases below. Effort (Axis 3) is orthogonal to both — it doesn't change WHICH model runs, only how much reasoning depth that model applies.

---

## Axis 1 — Production depth (Haiku / Sonnet / Opus)

### Haiku — fast, cheap
Correctness is easily verified. Errors are cheap and caught immediately.

**Use for:**
- File reads, grep, directory listings, content extraction
- Formatting, reformatting, restructuring known content
- Simple string transforms, renaming, sorting
- Lookup tasks with deterministic right answers
- Summarizing a document already in context
- Batch collection: fetch 10 URLs → one Haiku agent

**Agent call:** `model: "haiku"`

---

### Sonnet — balanced (default)
Needs judgment, but established patterns exist. Error caught in review.

**Use for:**
- Standard feature implementation and bug fixes
- Code review of known patterns
- Test writing for understood logic
- Explanations, documentation, copy
- Routine debugging with clear symptoms
- Most data pipeline and migration tasks
- UI implementation following existing design system

**Agent call:** `model: "sonnet"` — set it explicitly. Omitting `model:` does NOT default to Sonnet; it defaults to `inherit` (same model as the calling context) [VERIFIED: code.claude.com/docs/en/sub-agents, frontmatter table: the default is `inherit`, i.e. the main conversation's model].

---

### Opus — deep, expensive
Wrong answer has high downstream cost. Novel problem or high stakes. **The workhorse for all serious production depth** — architecture, security, strategy authoring, hard debugging.

**Use for:**
- Architecture decisions with long-term lock-in
- Security audits (wrong = breach or compliance failure)
- Strategic analysis and authoring (wrong = months of wrong direction)
- Complex debugging with unclear root cause or systemic cause
- Novel problem-solving with no established pattern
- Anything where "good enough" is not good enough

**Agent call:** `model: "opus"`

---

## Axis 2 — Fable (adjudication, alias `fable`)

Fable is the independent adversarial judge and the strongest model in this routing scheme. It is a **different model architecture** than Opus, so it breaks the "Opus reviews Opus" blind spot: a build reviewing itself shares its own bias. Fable costs more and is slower than the other models, so it is reserved for adjudication of high-stakes work and is **never set as the chat/session model**. Draw it deliberately, not by reflex.

**Three cases — and only these:**

1. **Adversarial gate.** A critic / contrarian / pre-mortem check on a plan (`/plans/`) or a high-stakes architecture / strategy decision. This is Fable's home ground and fires regularly in this workflow. **Wired into the `critic` and `contrarian` agents** via `model: fable` — dispatch them and the check runs on the right model, no per-call routing. Use the **alias `fable`**, never a full model ID — a pinned full ID in frontmatter fell back silently to the inherited model (observed in the maintainer's own session transcripts; not reproducible from this export). The alias is the canonical spelling everywhere: subagent frontmatter, Workflow `agent()` calls, and the CLI `--model` flag all take the same value space. Division of labour: **Opus plans and builds, Fable checks plan and build** — two models on the same artifact, checking each other. If Fable is unavailable, fall back to the **alias `opus`** — never a full Opus ID, which falls back silently to inherited: the same trap as for Fable.
2. **Irreversible fork.** Lock-in choices, decisions to abandon a project — anything where a wrong call costs weeks and is hard to see from inside. Fable delivers the second, independent verdict over Opus's proposal.
3. **Complex from the start.** A task so novel/hard that even Opus's best is not safely enough — here Fable may *produce*, not just judge. Judgment call, rare, never automatic.

**Not Fable:** routine build/fix, standard code review, research/extraction, strategy *authoring* (that's Opus → a contrarian judges it afterward). If you are reaching for Fable on the production path, you are almost certainly wrong — go Opus.

**Agent call:** `model: "fable"` — the alias is the canonical spelling. Do not write a full ID here. Which release the alias currently points at is a time-fact, not part of the routing rule; see [Alias resolution — measured state](#alias-resolution--measured-state) at the bottom and re-measure rather than quoting it.

---

## Axis 3 — Effort

Orthogonal to axes 1 and 2: effort does not change WHICH model runs, only how much reasoning depth it applies. Scale **low / medium / high / xhigh / max** — which levels are available depends on the model [VERIFIED: code.claude.com/docs/en/sub-agents, frontmatter table]; unsupported levels fall back to the highest supported level below (no silent break, documented behavior).

**Three places to set it** (no verified strict precedence chain between them — see the caveat below):

1. **Session default.** Applies to the main loop and to every subagent that sets nothing of its own (`inherits from session`) [VERIFIED: code.claude.com/docs/en/sub-agents, frontmatter table].
2. **Subagent frontmatter `effort:`.** Overrides the session default for that one agent type, permanently (agents/*.md) [VERIFIED: same table, "Overrides the session effort level"]. **Frontmatter is cached** — an edit only takes effect after the session restarts, exactly like the `model:` field.
3. **Workflow `agent()` param** (`opts.effort`). Overrides the session default for a single call inside a Workflow script [VERIFIED: the Workflow tool schema — "omit to inherit the session effort"; no hit in the public docs pages that were reachable, so the tool schema is the source, not cross-checked externally]. Whether a Workflow `agent()` call without `opts.effort` reads the target's frontmatter pin (2) or falls straight back to the session effort is **[PROPOSAL: unverified]** — the tool schema names only the session fallback, not the interaction with frontmatter pins.

**Rule:** `low` for mechanics/extraction (fetch, grep, reformat — deterministic tasks with no room for judgment); `high`/`xhigh` for hard verification and judge work (critic, contrarian, complex debugging chains). `max` only if even `xhigh` was visibly too tight.

**Caveat:** an `effort` pin at `high` for critic/contrarian is insurance, not a gain, if the target model (e.g. Fable) already defaults to `high` — the pin then only prevents a future lowering of the session default and changes nothing about the status quo.

---

## Adjudication hardening (permanent, independent of the judge model)

If judge and builder are the same model, the "Opus reviews Opus" blind spot appears. Even with Fable as judge, independence has to be enforced structurally and not only through model diversity — an outage must not take review sharpness with it. Four rules:

1. **Judge ≠ builder context.** The judge is NEVER the thread or model instance that built the artifact. Adjudication always runs as a **fresh subagent with empty context** — it sees only the artifact and the review assignment, not the build history and the justifications baked into it. A builder judging its own work shares its blind spots even with a different prompt.
2. **Explicit artifact-verification assignment.** Every critic/contrarian assignment contains the hard requirement: *"Verify every reality claim directly against the artifact before you accept it — cite line number / file path, do not infer from the assignment text."* Without this, the judge takes the requester's claims as fact: plausibility instead of verification.
3. **Verdict cross-check.** The judge's verdict claims are **spot-checked against the artifact** before they become actionable. The judge is not the last instance — a false-positive "BLOCKER" or a missed real defect only becomes visible through re-checking.

4. **Mandatory evidence tagging.** Every critic/reviewer assignment additionally requires: *"Tag EVERY statement either with `[VERIFIED: <command / file:line>]` or with `[PROPOSAL: unverified]`. A remedy you did not execute is ALWAYS `[PROPOSAL]`, even if you verified something else in the same paragraph."* Reason: a diff reviewer **with** Bash can verify findings empirically (run the suite, plant mutations) and, in the same paragraph, propose a fix that relies on a field that does not exist in the data model. The finding is verified, the remedy is guessed, and **from the outside they look identical**; an unchecked claim like that can end up in a code comment on the main branch. Consequence for rule 3: `[PROPOSAL]` statements are checked **completely**, not by sample — the sample applies only to `[VERIFIED]`. Without the tag, rule 3 cannot be aimed: you do not know where the sample belongs.

These four rules apply in addition to Axis 2. In normal operation (Fable as judge) they reinforce cross-architecture adjudication; if Fable is unavailable they replace the lost model diversity. For irreversible forks the tiebreaker below and the cross-family gate (domains 1-4 below) also apply.

### Tiebreaker on reviewer disagreement

If two independent, NON-Fable reviewers (e.g. two skeptic/review dispatches, same or different tier) reach contradicting verdicts: a third agent, Fable, fresh context, bound by rules 1+2 above, must check the disputed claim against the artifact itself — no unchecked third opinion. If Fable is unavailable: Opus, same binding.

**If Fable is itself one of the disagreeing reviewers** (e.g. two critic/contrarian runs contradict each other): no automatic tiebreaker — a Fable tiebreak against a Fable dispute would be circular (shared prior, no independent second architecture left). Structurally the same case as domains 1-4 below → the decision goes to the maintainer.

### Cross-family gate (decorrelated reviewer ≠ builder family, domains 1-4)

Four domains in which the reviewer should NOT come from the same model family as the builder — the normal `critic`/`contrarian` (Fable) is often enough here already, because Fable is a different architecture than Opus. For the first two domains a third, independent architecture would be needed and is not available, so the four hardening rules above are the full extent of enforced independence:

1. **Harness edits.** Changes to the harness itself (hooks, agents, skills, routing SSOT) — builder and judge must not share the same blind spot for "my own config is fine".
2. **Project decision path.** Architecture lock-in decisions, anything involving real money.
3. **Verdicts.** A verdict that itself becomes actionable (meta level: a critic result that decides further build steps) — here you are not checking the artifact but the judgment about the artifact.
4. **Security.** Security-relevant changes (credentials, keys, auth, attack surface).

**Mechanizable as a fail-closed Write|Edit gate, but currently a convention, not enforcement** — not built, because a reliable automatic domain detector (is this edit a harness edit? a project decision?) would produce more false alarms than value without prior evidence from real use. Re-evaluation trigger: as soon as ≥2 real cases show the convention was missed.

### Fable fallback: content-correlated classifier refusal

A Fable turn can be served by Opus without any error surfacing to the caller. Transient causes (rate limit, overload) do not trigger an automatic switch by design; a first-party Anthropic guide documents a separate class of fallback [as reported by a first-party Anthropic guide, `fable_5_fallback_billing/guide.ipynb` in the claude-cookbooks repository; not re-checked for this export; paraphrased below]:

**Safety-classifier refusal.** Fable 5 runs automated safety checks on every request in three areas — offensive cybersecurity (exploits, malware, attack tooling), biology/life sciences, and extraction of the model's summarized thinking. A hit returns `stop_reason: "refusal"`; with fallback configured, the turn is automatically retried on the then-current Opus release (the guide named a specific Opus release, which is certainly stale now; the *mechanism* is the claim, not the number). The guide describes these safeguards as deliberately conservative, so benign technical work sometimes triggers them.

What makes this class structurally different: it correlates with **the content being judged**, not with account state or load. Not a residual probability — a systematic bias.

**Per-subagent, not per-session, and stated as already live in Claude Code** [same guide, anti-pattern section, paraphrased, not re-checked]: only the subagent that hits a refusal falls back to Opus; the rest of the session continues on Fable. Blast radius is one dispatch — but it is the dispatch that needed the independent architecture, and the surrounding session looks unaffected. **[PROPOSAL: unverified]** The guide speaks generically about Claude Code subagents; that this also applies to a frontmatter-pinned `model: fable` agent is an **extrapolation**, not a quoted claim.

**Consequence for the cross-family gate (domains 1-4 above):** a `critic` / `contrarian` dispatch on security-adjacent material is exactly the kind of content that **can** trip the cyber classifier — adjudicating a code or security review of a harness gate script, an auth/credential change, an attack-surface analysis. If it trips, the judge becomes Opus. That degrades **domain 4 (security)** and **domain 1 (harness edits)** — two of the four domains, precisely the security-correlated ones — and cuts against routing rule 7 below ("Never let Opus review Opus on an irreversible fork") in the cases that motivated it, with no error surfacing to the caller.

**Detection: possible in principle, unverified in this harness.** Per the guide, the serving model is reliably identifiable: `usage.iterations` is the reliable signal that a fallback model served the turn, and the response's `model` field names whichever model actually answered (the guide's explicit anti-pattern is building analytics from the *requested* model instead). One nuance matters for multi-turn runs: a **sticky-served** turn — routed straight to the fallback model because an earlier turn in the same conversation already fell back — carries **no `{"type":"fallback"}` content block**. The content block is therefore not a sufficient detector; the serving-model field and `usage.iterations` still are.

The open question is narrower than "can this be detected": it is whether the **Claude Code transcript exposes those fields per subagent dispatch** at all, and whether a guard script could read them. That is **[PROPOSAL: unverified]** and stays an open item.

**Operating rule until that is settled:** in domains 1 and 4, treat "the judge was Fable" as an assumption, not a fact. Where a verdict is handling-critical, confirm the serving model from the transcript before relying on cross-architecture independence — otherwise lean on the hardening rules above (fresh context, artifact-bound verification, `[VERIFIED]` / `[PROPOSAL]` tagging), which hold regardless of which model judged.

## Decision Matrix (Axis 1)

| Signal | → Haiku | → Sonnet | → Opus |
|--------|---------|----------|--------|
| Verb | fetch / find / list / extract | implement / fix / write / review | design / audit / decide / author |
| Correctness | deterministic | needs judgment | high-stakes judgment |
| Error cost | caught immediately | caught in review | caught in production or never |
| Domain knowledge | none needed | standard | novel or adversarial |
| Estimated time | <1 min | 1–10 min | 10–60 min |

**Verb trap:** the verb column is a heuristic, not an automatism. "review" maps to Sonnet by default — but a reviewer that sits on the harness's own security surface (hooks/scripts/agents that run autonomously, the last gate before an autonomous merge) has the error cost of the Opus column ("caught in production or never"), not of the Sonnet column. Check the actual blast radius first, then apply the verb default. Concrete case: `code-reviewer` (the local harness reviewer) runs on `model: opus` for exactly this reason, although the verb column alone would suggest Sonnet.

**No Opus tier for implementation:** this setup has no dedicated Opus implementer. For "complex debugging with unclear root cause" (Opus column above), dispatch the existing implementer with an explicit Opus override (set `model` on the agent call) instead of creating a new agent.

**Model caching applies session-wide, not only per agent frontmatter:** the global `model` field in `settings.json` (e.g. `opusplan`) is also read only when a process starts [VERIFIED: code.claude.com/docs/en/settings — read once at session start, applied on the next restart]. A project-local `settings.json` without its own `model` field falls back to the user value under the standard precedence (Managed > CLI > Local > Project > User) — but only for processes started AFTER the change. A Claude Code process that is already running in another project keeps its previously cached model until it is restarted — verified: sessions started before the change kept running on their old cached state until they were explicitly restarted.

**Fable is off this matrix** — it is the adjudication axis, not a depth column. Trigger it by *job* (judging a plan/decision, or an irreversible fork), not by the signals above.

## Cost & routing rules

1. **Never use Opus** where Sonnet answers correctly.
2. **Never use Sonnet** where Haiku answers correctly.
3. **Batch Haiku tasks:** 5 simple lookups → one agent, not five.
4. **Opus is justified when:** `cost_of_wrong_answer >> model_cost_delta`
5. **Parallel Haiku:** independent extraction tasks run concurrently in one message.
6. **Fable costs more and is slower than Opus** — reserve it for adjudication gates and irreversible forks, never as a production default.
7. **Never let Opus review Opus on an irreversible fork** — route the check to Fable; same-architecture self-review shares the blind spot.

## Multi-Step Task Examples

Production task — no adjudication needed:
```
Task: "Add a batch of rows to a data file, validate, commit"

Step 1 – Parse and normalize input         → Haiku
Step 2 – Check for key conflicts in JSON   → Haiku
Step 3 – Edit the data file                → Sonnet
Step 4 – Run the validate script           → Bash (no subagent)
Step 5 – Verify row counts afterwards      → Haiku
Step 6 – Write commit message              → Sonnet
```

Irreversible fork — Opus builds, Fable judges:
```
Task: "Decide the new system architecture for X" (long-term lock-in)

Step 1 – Research existing patterns            → Haiku
Step 2 – Draft architecture + tradeoffs        → Opus
Step 3 – Contrarian pre-mortem on the draft    → Fable (adjudication gate)
Step 4 – Revise per surviving objections       → Opus
```

## Output format when routing

When applying this skill to a plan, annotate each step:

```
Step 1: [Haiku]  Fetch README from GitHub
Step 2: [Sonnet] Implement matching filter
Step 3: [Opus]   Design the architecture
Step 4: [Fable]  Adversarial gate before commit
Step 5: [Haiku]  Verify file counts
```

## Caveats

- **Aliases are the canonical spelling** (`haiku` / `sonnet` / `opus` / `fable`) in frontmatter, in Workflow `agent()` calls and on the CLI `--model` flag. They self-update to the current release of their family. Write a full model ID only where an alias genuinely cannot express the target (pinning a legacy release on purpose, provider-format IDs for Vertex/Bedrock/gateway) — and never from memory: read it off the provider docs or the model picker at the moment you write it.
- Haiku on genuinely complex tasks produces confident wrong answers — worse than Sonnet.
- Opus on simple tasks wastes latency and budget, but is not unsafe.
- When uncertain between depth tiers: go one tier up, not down.
- **Fable ≠ deeper Opus.** Using it as a bigger hammer on production work wastes latency and misses the point — its value is *independent* judgment, not more depth. If Fable is unavailable, fall back to the alias `opus` for the adjudication cases — not a full Opus ID, which falls back silently to inherited (same trap as the Fable alias).

---

## Alias resolution — measured state

Time-facts. They go stale; the routing rules above do not. Never quote a line
from this section as current truth — re-measure with the method below.

| Alias | Resolved to | Measured |
|-------|-------------|----------|
| `opus` | `claude-opus-5-5` | when this was written (autumn 2026) |

**Method (reproducible):** dispatch a throwaway subagent with `model: opus`,
then grep the session transcript JSONL for the served model field of that
dispatch. The requested value is not evidence — read the *served* model.

**An alias can lag its family's newest release.** It was observed once that the
`fable` alias kept serving the previous Fable release for a while after a newer
one had shipped. So "alias" means self-updating, not instantly-newest. If a
specific release is genuinely required, measure first — do not assume the alias
has caught up, and do not fix the problem by pinning a full ID into
frontmatter, which falls back silently to inherited.

**`best` is not in use and must not be introduced.** It can resolve to Fable, so
it would silently route production subagents onto the model that is reserved for
adjudication, against the policy above.
