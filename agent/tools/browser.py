"""Read approved Brew & Co. pages and submit trusted drafts through a real browser."""

from pathlib import Path
from datetime import date
from typing import Callable
from urllib.parse import urlparse
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from agent.contracts import ToolResult, ToolStatus


class OrderSubmissionDraft:
    """Trusted submission fields supplied by the deterministic draft store."""

    def __init__(self, decision: str, supplier_id: int, sku: str, quantity: int,
                 expected_delivery: str, unit_price_inr: int, approval_valid: bool = False):
        self.decision = decision
        self.supplier_id = supplier_id
        self.sku = sku
        self.quantity = quantity
        self.expected_delivery = expected_delivery
        self.unit_price_inr = unit_price_inr
        self.approval_valid = approval_valid


class BrewBrowserTool:
    """A read-only browser client restricted to approved local data pages."""

    PAGES = {
        "dashboard": ("/", None),
        "inventory": ("/inventory", "inventory-row"),
        "suppliers": ("/suppliers", "supplier-row"),
        "purchase_orders": ("/purchase-orders", "purchase-order-row"),
    }

    def __init__(self, base_url: str = "http://127.0.0.1:5000", timeout_ms: int = 4000,
                 evidence_dir: str | Path | None = None,
                 draft_lookup: Callable[[str], OrderSubmissionDraft | None] | None = None):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("browser tool may connect only to a local Brew & Co. server")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("base URL must contain only the local server origin")
        self.base_url = base_url.rstrip("/")
        self.timeout_ms = timeout_ms
        self.evidence_dir = Path(evidence_dir) if evidence_dir else (
            Path(__file__).resolve().parents[2] / "runs" / "browser_evidence"
        )
        self.draft_lookup = draft_lookup

    def read_dashboard(self) -> ToolResult:
        return self._read_page("dashboard")

    def read_inventory(self) -> ToolResult:
        return self._read_page("inventory")

    def read_suppliers(self) -> ToolResult:
        return self._read_page("suppliers")

    def read_purchase_orders(self) -> ToolResult:
        return self._read_page("purchase_orders")

    def submit_purchase_order(self, draft_id: str) -> ToolResult:
        if not isinstance(draft_id, str) or not draft_id.strip():
            return ToolResult(ToolStatus.ERROR, error_code="invalid_draft_id",
                              message="A draft ID is required.")
        draft = self.draft_lookup(draft_id) if self.draft_lookup else None
        if draft is None:
            return ToolResult(ToolStatus.ERROR, error_code="draft_not_found",
                              message="No trusted order draft exists with that ID.")
        if draft.decision == "blocked":
            return ToolResult(ToolStatus.BLOCKED, error_code="policy_blocked",
                              message="A blocked draft cannot be submitted.")
        if draft.decision == "needs_approval" and not draft.approval_valid:
            return ToolResult(ToolStatus.NEEDS_APPROVAL, error_code="approval_required",
                              message="This draft needs valid owner approval before submission.")
        if draft.decision not in {"permitted", "needs_approval"}:
            return ToolResult(ToolStatus.BLOCKED, error_code="invalid_draft_decision",
                              message="The draft is not authorized for submission.")
        try:
            if (not isinstance(draft.supplier_id, int) or draft.supplier_id < 1
                    or not isinstance(draft.quantity, int) or draft.quantity < 1
                    or not isinstance(draft.unit_price_inr, int) or draft.unit_price_inr < 1
                    or not draft.sku):
                raise ValueError
            date.fromisoformat(draft.expected_delivery)
        except (TypeError, ValueError):
            return ToolResult(ToolStatus.BLOCKED, error_code="invalid_draft_fields",
                              message="The trusted draft has invalid submission fields.")

        write_attempted = False
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, timeout=self.timeout_ms)
                try:
                    page = browser.new_page()
                    self._restrict_routes(page)
                    response = page.goto(f"{self.base_url}/new-purchase-order",
                                         wait_until="domcontentloaded", timeout=self.timeout_ms)
                    if response is None or response.status >= 400:
                        return ToolResult(ToolStatus.ERROR, error_code="form_unavailable",
                                          message="The new purchase-order form is unavailable.")
                    page.locator('[data-testid="supplier-id"]').select_option(str(draft.supplier_id))
                    page.locator('[data-testid="sku"]').select_option(draft.sku)
                    page.locator('[data-testid="quantity"]').fill(str(draft.quantity))
                    page.locator('[data-testid="expected-delivery"]').fill(draft.expected_delivery)
                    expected_total = draft.quantity * draft.unit_price_inr
                    estimate_text = page.locator('[data-testid="price-estimate"]')
                    expected_label = f"Estimated total: ₹{expected_total:,}"
                    page.wait_for_function(
                        "label => document.querySelector('[data-testid=price-estimate]')?.textContent.includes(label)",
                        arg=expected_label, timeout=self.timeout_ms,
                    )
                    write_attempted = True
                    with page.expect_navigation(wait_until="domcontentloaded", timeout=self.timeout_ms) as navigation:
                        page.locator('[data-testid="submit-order"]').click()
                    submitted = navigation.value
                    if submitted is None or submitted.status == 504:
                        return ToolResult(ToolStatus.UNKNOWN, error_code="unknown_outcome",
                                          message="The app response was lost or failed after submission; verify state before retrying.")
                    if submitted.status >= 400:
                        return ToolResult(ToolStatus.ERROR, error_code="submit_rejected",
                                          message="The app rejected the purchase-order form.")
                    receipt = page.locator('[data-testid="po-receipt"]')
                    if receipt.count() != 1:
                        return ToolResult(ToolStatus.UNKNOWN, error_code="receipt_missing",
                                          message="Submission returned without a purchase-order receipt.")
                    po_number = receipt.inner_text().split("—", 1)[0].strip()
                    screenshot = self._save_screenshot(page, "submitted-order")
                    return ToolResult(
                        ToolStatus.OK,
                        data={"draft_id": draft_id, "po_number": po_number, "status": "Placed",
                              "supplier_id": draft.supplier_id, "sku": draft.sku,
                              "quantity": draft.quantity, "unit_price_inr": draft.unit_price_inr,
                              "expected_delivery": draft.expected_delivery},
                        evidence={"url": page.url, "po_number": po_number, "screenshot": str(screenshot)},
                    )
                finally:
                    browser.close()
        except PlaywrightError as exc:
            status = ToolStatus.UNKNOWN if write_attempted else ToolStatus.ERROR
            code = "unknown_outcome" if write_attempted else "browser_error"
            return ToolResult(status, error_code=code, message=f"Browser submission failed: {exc}")

    def _save_screenshot(self, page, prefix: str) -> Path:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        screenshot = self.evidence_dir / f"{prefix}-{uuid4().hex[:10]}.png"
        page.screenshot(path=str(screenshot), full_page=True)
        return screenshot

    def _restrict_routes(self, page) -> None:
        allowed_origin = (urlparse(self.base_url).scheme, urlparse(self.base_url).netloc)
        allowed_paths = {path for path, _selector in self.PAGES.values()}
        allowed_paths.update({"/new-purchase-order", "/order-estimate"})
        def handle(route):
            parsed = urlparse(route.request.url)
            if (parsed.scheme, parsed.netloc) == allowed_origin and parsed.path in allowed_paths:
                route.continue_()
            else:
                route.abort()
        page.route("**/*", handle)

    def _read_page(self, page_name: str) -> ToolResult:
        path, test_id = self.PAGES[page_name]
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, timeout=self.timeout_ms)
                try:
                    page = browser.new_page()
                    self._restrict_routes(page)
                    response = page.goto(f"{self.base_url}{path}", wait_until="domcontentloaded",
                                         timeout=self.timeout_ms)
                    if response is None or response.status >= 400:
                        status = response.status if response is not None else "no response"
                        return ToolResult(ToolStatus.ERROR, error_code="page_unavailable",
                                          message=f"The {page_name} page returned {status}.")
                    final = urlparse(page.url)
                    if final.netloc != urlparse(self.base_url).netloc or final.path != path:
                        return ToolResult(ToolStatus.BLOCKED, error_code="page_outside_allowlist",
                                          message="Navigation left the allowed dashboard page.")
                    screenshot = self._save_screenshot(page, page_name)
                    rows = (page.locator(f'[data-testid="{test_id}"]').all_inner_texts()
                            if test_id else [])
                    return ToolResult(
                        ToolStatus.OK,
                        data={"page": page_name, "rows": rows,
                              "content": page.locator("body").inner_text()[:12_000],
                              "content_label": "UNTRUSTED PAGE CONTENT"},
                        evidence={"url": page.url, "title": page.title(),
                                  "screenshot": str(screenshot)},
                    )
                finally:
                    browser.close()
        except PlaywrightError as exc:
            return ToolResult(ToolStatus.ERROR, error_code="browser_error",
                              message=f"Dashboard browser read failed: {exc}")
