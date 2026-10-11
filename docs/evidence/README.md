# Final-run evidence

This folder is reserved for sanitized evidence from successful final real runs. Two live E02 attempts used isolated seeded databases. The first exposed missing Gemini 3 thought signatures and function-call IDs in manually rebuilt history. After fixing that, the second run completed six Gemini turns: it read inventory through Chromium, checked open orders, compared supplier options, read the ordering policy, and created a permitted TEA-ASM draft for 6 kg from S3 at ₹3,120. The following model request returned HTTP 429, so no purchase order was submitted and no verifier result exists. The corrected multi-turn flow has progressed through draft creation, but a successful live browser submission remains pending quota availability. Local diagnostics are under the ignored `runs/final-live-e02-20261011/`, `runs/final-live-e02-signature-fix-20261011/`, and `runs/browser_evidence/` directories. Do not copy `.env`, API keys, or unsanitized local data here.

For the final E02 run, save:

- a sanitized event-log excerpt showing model selection, tool calls/results, PO number, verifier checks, and final status;
- the browser screenshot evidence for the purchase-order receipt;
- the exact test/run command and observed result in the evaluation table in `README.md`.

Scripted-model test output is not a substitute for real-run evidence.
