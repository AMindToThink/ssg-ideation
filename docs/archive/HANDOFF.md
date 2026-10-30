> **Status: done (2026-10-30).** These handoff notes date from before implementation, and their "not started" and next-step instructions are out of date. Implementation is complete; see the README.

# Handoff notes (planning session, 2026-10-28)

Planning is done and implementation has **not started**. `PLAN.md` is the approved plan. To continue on a new machine:

1. Clone this repo.
2. Create `.env` with `OPENROUTER_API_KEY=...`. It is git-ignored and was deliberately not committed.
3. Tell Claude: "Implement PLAN.md; see HANDOFF.md for decisions already made."

## Decisions Matthew made during planning (don't re-ask)
- **Models:** base models only (no `-pro`, no Fusion). Max reasoning effort wherever it's available:
  - Astra, Opus 5.5 and Fable 5.1 run at `max`.
  - Gemini 4 Argon runs at `high`, its highest supported effort.
- **System prompt:** do nothing weird or off-policy. Anthropic's docs say the claude.ai prompts "do not apply to the Claude API". OpenRouter recommends no system prompt. So: **no system prompt for any model**. Matthew asked us to confirm that the API adds no default prompt. Evidence:
  - The overview text in `reference/claude_ai_system_prompts/*.md` (the header of Anthropic's system-prompts page) says the prompts are for claude.ai and the apps only.
  - Anthropic's context-awareness docs: Opus 4.7+ and Fable 5.1 get no injected budget tags.
  - The automatic tool-use system prompt only applies when `tools` are sent, and we send none.
- **Judge:** binary, at the bar the Appendix sets:
  - **Counts:** Astra+fusion's "agents become one another's trainers / impose a competing RL objective".
  - **Does not count:** "agents learn reputation and cooperation systems".
  - The judge model is `deepseek/deepseek-v4.1-flash-1017`.
- **Rank:** per turn. Example: "Turn 1, #4 of 12".
- **Repo:** private, with `.env` ignored. Push when done (Matthew asked for this explicitly).

## Facts checked against OpenRouter on 2026-10-28
- **First-party endpoints exist:** `openai` for gpt-6-astra, `anthropic` for opus-5.5 and fable-5.1, `google-ai-studio` for gemini-4-argon, and `deepseek` for the judge.
- **Claude 5.5 and Fable:** non-default temperature, top_p and top_k are rejected. Thinking is adaptive and cannot be disabled.
- **Gemini 4 Argon:** the per-request output cap is 65,536 tokens. Beyond that it uses "long decode" continuation tokens; see the OpenRouter Gemini 4 migration guide.
- **Opus 5.5 guardrail:** the post's footnote says Opus 5.5 hit cyber guardrails before. Record refusals as an outcome and don't retry them.

## Reference files
- `reference/ssg_post_draft.txt`: plain-text export of the Google Doc draft, which is the source of the verbatim prompts and the appendix bar.
- `reference/claude_ai_system_prompts/`: Anthropic's published claude.ai prompts for Opus 5.5 and Fable 5.1. They are kept for provenance only and are **not used**.
