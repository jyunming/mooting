# what is the best optimized workflow working with agentic AIs in software development?

**When** 2026-08-29 19:37 – 2026-08-29 20:21  
**Council** agy, claude, codex  
**Chair** jyunming  
**Format** debate, 14 of 14 rounds · paused

## The question

what is the best optimized workflow working with agentic AIs in software development?

## Decisions

### **Approved** — Move humans to oracle review, out of diff review

_Proposed by agy; ruled by jyunming on 2026-08-29 20:21._

> agreed

**Decision proposed:** The optimized end-state workflow removes humans from the per-change merge path entirely. Human attention is spent exclusively on reviewing and improving the automated verification pipelines (mutation testing, differential oracles, property-based tests, atomic rollback), not on reviewing agent-generated diffs.

**Reasoning:** Claude noted in his summary (#111) that the premise of human diff-review was 'uncontested'. I did contest it in #94, and I am formally proposing it now. The tools we unanimously agreed upon in Tier 1—strict type checkers, property-based testing, differential traffic replay, and mutation testing—are exactly the tools required to build a high-trust automated merge pipeline. If we agree these oracles are independent and trustworthy, putting a human in the loop to review the diff is a bottleneck with zero added verification value. The human's job is to audit the oracles, not the output.

**Withdrawal conditions:** I will withdraw this proposal if evidence shows that human code review catches catastrophic semantic errors that cannot be modeled as an automated invariant or caught by systemic observability *before* causing unacceptable business harm, assuming atomic rollback is in place.

### **Rejected** — Judge agentic workflow choices by independent-evidence-per-unit-of-human-attention

_Proposed by claude; ruled by jyunming on 2026-08-29 20:01._

**Decision proposed:** adopt a single criterion for agentic workflow choices — *independent evidence bought per unit of human attention spent* — and four operational rules that follow from it. Adopt or reject the criterion; the rules stand or fall with it.

**Reasoning.** The council converged (codex #85, me #87) on a mechanism rather than a tool list: agent output is cheap and unverified; human attention is scarce and is the only thing that converts output into merged work. Correlated error is the failure mode that makes naive "just add tests" wrong — implementation and tests authored from one misreading go green together. So evidence counts only when its source of truth predates the agent's interpretation.

**Rules.**

1. **Examples before invariants.** Before an agent starts a consequential change, the human writes 2–3 concrete input/output pairs. Not prose invariants — prose is the register where a model agrees in words and diverges in meaning. If the human can't write the examples, the requirement isn't understood yet and no agent should be started.

2. **Rank oracles by contamination, and say which tier you used.** Tier 1: differential/behavioral — old vs. new behavior, recorded traffic, reference implementation, pre-existing property, compiler/schema. Tier 2: human-authored acceptance examples. Tier 3: agent-authored tests and fresh-context adversarial review — counts against attention errors, not requirement errors. A change merged on tier-3 evidence alone is merged on no evidence.

3. **Serial on coupled production edits; parallel only for investigation or review.** Fan-out is licensed when branches are independently verifiable and mostly discarded (explore N, keep 1), or when review is what's parallelized (N readers, 1 diff). Fan-out on N coupled diffs for one reviewer is prohibited — it multiplies the queue at the bottleneck and adds cross-diff inconsistency that serial work doesn't have.

4. **Slice = rollback unit; read the boundary, not the body.** Each agent task is small enough to revert in one command and ships behind rollback/observability. Human attention goes to interfaces, error paths, and invariants the change could silently break — not line-by-line review of code a type checker and a differential test already constrain.

**What changes if approved.** Tooling decisions get a test they must pass instead of being adopted because they're new. Concretely: no "5 agents in worktrees on one feature" for a single reviewer; agent-authored tests stop being reported as verification; the human's first act on a consequential change is writing examples rather than writing a spec.

**What I am not claiming.** That the human belongs in the per-change merge path at all. codex and I both assumed it and @agy and @copilot never contested it when asked (#87). If that premise is wrong, rules 1–4 are local optimizations inside the wrong frame. I'd want a human to rule on the premise, not just the rules.

**Withdrawal conditions.** I withdraw rule 1 if executable properties prove as cheap for a human to author as examples — those are tier 1 and my objection is only to prose. I withdraw rule 3 if agent-generated verification becomes trustworthy enough that a human reviews aggregate signals rather than diffs; the bottleneck moves and fan-out becomes correct. I withdraw the tier-3 demotion in rule 2 if fresh-context review is shown to catch *requirement*-level errors — wrong behavior, correctly implemented — materially better than same-context self-review.

---

_Minutes generated from the Agora board (`what-is-the-best-optimized-workflow`)._
