# Pre-submission checks — 12 October 2026

## Scenario rehearsals

Four focused offline checks passed (`4 passed in 1.18s`):

- E04 approval: the task pauses for owner approval, then the PO is created and independently verified.
- E04 rejection: the draft is rejected and the seeded total remains eight purchase orders.
- E06 covered order: existing PO-1001 covers caramel syrup; no new draft or PO is created.
- Dashboard approval: the approved order resumes and the dashboard displays the verified result.

These rehearsals use the scripted model and make no Gemini API calls.

## Clean checkout smoke check

The committed `main` checkout matching `origin/main` was cloned locally into a temporary directory. Following the README seed command succeeded. The dashboard, inventory, suppliers, purchase orders, new purchase-order form, and `/static/style.css` all returned HTTP 200. The dashboard showed seeded PO-1007, `.env.example` was present, and `.env` was absent.

The temporary clone and test scratch directory were removed after verification. The source checkout was clean during the smoke check; the evidence and README status updates were added afterward.
