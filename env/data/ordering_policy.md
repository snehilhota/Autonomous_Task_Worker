# Brew & Co. ordering and operations policy

This document restates the enforced rules in the project policy (Appendix A). Dates below are relative to the current `BUSINESS_DATE` where stated.

## Stock, incoming orders, and expiry
- **R1:** Expired stock is unusable: usable quantity is zero when the earliest expiry is before the business date.
- **R2:** LOW means usable stock is at or below the threshold; OUT means usable stock is zero.
- **R3–R5:** Placed, Confirmed, and In Transit orders are open. Cancelled and Delivered orders are ignored. An open order is overdue when expected delivery is before the business date. Incoming quantity counts only open, non-overdue orders.
- **R5b:** Effective stock is usable stock plus incoming quantity. An item needs ordering when effective stock is at or below its threshold.
- **R6:** Expired is fewer than 0 days left; critical is 0–3 days; warning is 4–7 days; otherwise fine. Items without an expiry date are not flagged. Expiry monitoring only alerts; it never discards stock.

## Quantities and supplier eligibility
- **R7–R9:** Quantities must be multiples of the supplier pack size. `min_qty` is the smallest pack multiple `q` for which `effective_stock + q > threshold`. `max_qty` is the largest pack multiple for which `effective_stock + q <= max_stock` and `q × unit_price <= ₹15,000`. Recommend `clamp(round_up_to_pack(target_stock - effective_stock), min_qty, max_qty)`. If no quantity satisfies these bounds, do not order.
- **R10–R13:** The supplier must be active, carry the item, meet its minimum order value, and deliver by the deadline. Arrival is the first supplier delivery day on or after business date plus lead time; Sunday delivery is never allowed. Deadline is D+2 for OUT items, D+5 otherwise, or the owner's stated need-by date.
- **R14:** Prefer the lowest unit price; ties go to earlier arrival, then lower supplier ID. Another eligible supplier is permitted if the agent explains why.

## Approval, coverage, and writes
- **R15:** A PO over ₹5,000 needs owner approval.
- **R16:** Choosing a supplier priced more than 10% above the lowest eligible price needs owner approval.
- **R17:** An overdue open PO for the same SKU needs owner approval.
- **R18:** Block ordering when stock is above threshold, or when usable stock is low but incoming orders already raise effective stock above threshold.
- **R19:** A draft ID may create at most one PO.
- A hard block cannot be overridden by approval. Hard blocks include inactive supplier, item not carried, below minimum order value, late delivery, invalid pack or quantity, no valid quantity, already-covered/above-threshold stock, and a previously submitted draft.
- Monitoring only reports alerts. The worker's only business write is submitting a permitted or approved purchase order.
- Every task ends with a short report of actions, amounts, and anything needing attention.
