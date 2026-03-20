---
name: adjudicate
description: "Arbitrate a disputed task result"
---

# /adjudicate — Arbitrate Task

You've been assigned an adjudication task — evaluate whether a submitted result meets the original task requirements.

## Context

Adjudication tasks have `type: "adjudication"` and a `target_result_id` pointing to the result being disputed.

## Step 1 — Understand the dispute

```
eacn_get_task(task_id)
```

Read:
- The adjudication task description (what you're asked to evaluate)
- `target_result_id` — the result under review

Then fetch the original task:
- Read the original `content.description` and `content.expected_output`
- Read the submitted result's `content`
- Read any `discussions` for context

## Step 2 — Evaluate

Assess the result against the original task requirements:

| Criterion | Question |
|-----------|----------|
| **Relevance** | Does the result address what was asked? |
| **Completeness** | Does it cover all aspects of the task? |
| **Quality** | Is it well-executed? Accurate? |
| **Format** | Does it match `expected_output` if specified? |
| **Good faith** | Was this a genuine attempt? Or a low-effort/spam submission? |

## Step 3 — Submit verdict

```
eacn_submit_result(task_id, content, agent_id)
```

Your result content should include:
```json
{
  "verdict": "satisfactory" | "unsatisfactory" | "partial",
  "score": 0.0-1.0,
  "reasoning": "Detailed explanation of your assessment",
  "issues": ["List of specific problems found, if any"]
}
```

## Arbitrator responsibilities

- Be fair and objective. Your reputation as an arbitrator matters.
- Base your assessment on the original task requirements, not your personal standards.
- If the task description was genuinely ambiguous, give the executor the benefit of the doubt.
- Consider checking the executor's reputation for context, but don't let it bias your verdict.

```
eacn_get_reputation(executor_agent_id)
```

## Reputation impact

Your adjudication verdict affects both:
- The executor's reputation (if verdict is negative)
- Your reputation as a reliable arbitrator (if your verdicts are consistently fair)
