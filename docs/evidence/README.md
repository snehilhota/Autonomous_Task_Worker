# Final-run evidence

`e02-live-20261012.md` records the successful live E02 run and the checks performed by the independent verifier. `e02-po-1009.png` is the receipt screenshot captured by the real local browser during submission.

The run used an isolated seeded database. The configured primary model returned transient HTTP 503 errors, so the successful run used the already-configured Gemini 3.6 Flash model alone. Requests were serialized with a minimum 1.5-second gap. No API key or `.env` contents are included in this evidence.
