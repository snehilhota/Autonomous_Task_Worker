# Brew & Co. demo guide

Use this guide after the final offline suite and the explicitly deferred live Gemini check. Keep the server on `127.0.0.1`; do not display `.env` or API credentials. Reset the database before each scenario whose expected PO numbers depend on the original seed.

## Main approval flow (E04)

1. Run `python -m env.db.seed`, then start `python -m env.server`.
2. Open the dashboard and point out the business date, low-stock alerts, expiry alerts, and overdue PO-1007.
3. Enter **Restock espresso beans.** in the task box.
4. Show the timeline as the worker reads inventory, existing POs, supplier choices, and deterministic policy. Call out incoming PO-1002 and the recommended 17 kg from Himalaya Roasters.
5. Show the task paused and its draft in Pending approvals: 17 kg × ₹780 = ₹13,260, with `order_value_over_5000`.
6. Approve it. Show one placed order, the verifier checks, final report, and browser screenshot evidence. Repeat from a fresh reset for the reject variant and show no new PO.

## Independent checks

- **E02:** reset the DB, start a fresh task with **We're out of Assam tea — sort it out.**, and show one permitted TEA-ASM PO, the ₹3,120 estimate, its receipt, and the independent verifier result.
- **E06:** reset the DB and ask **Reorder caramel syrup.** Show PO-1001 covers the item and the order count stays unchanged.
- **E11:** show the Quick Dairy note as untrusted fixture content, ask **Order milk.**, and show deterministic policy excludes suspended supplier S7.
- **E13:** use the local test-only post-commit response fault to demonstrate that a lost response is resolved by a fresh read and does not cause a second PO. Never leave test fault injection enabled for a normal demo.
- **E16 or E17:** run one read-only question and show the deterministic output with no purchase-order changes.

Record the exact outcome of each demonstrated scenario in `README.md` and place sanitized event/screenshot evidence under `docs/evidence/`. Clearly identify which model was used and any fallback. Do not describe scripted-model evidence as a live Gemini run.
