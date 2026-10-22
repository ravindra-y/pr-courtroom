# Demo Repo: `shop-cart`

This folder contains the sample Python project used to demonstrate **PR Courtroom**.

## Demo Pull Request Scenario (PR #42)
- **Title:** Add tiered promo code discounts and refactor subtotal calculation
- **Changed file:** `shop/cart.py`
- **What changed between `snapshots/base` and `snapshots/head`:**
  1. Added `apply_discount(subtotal, promo_code, is_vip=False)`:
     - Docstring states: *"Total after discount cannot be negative (minimum 0.0)."*
     - **Bug (New-code defect):** `"FLAT50"` subtracts `50.0` without clamping to `0.0`, producing negative totals for small carts.
     - **Documented Intent:** PR description specifies that VIP 5% discount is applied *after* the promo code discount (compound: `subtotal * 0.80 * 0.95 = 76.0`), not additively (`75.0`).
  2. Refactored `calculate_subtotal(items)`:
     - **Bug (Regression):** Accidentally dropped `* item.get("quantity", 1)` when summing item prices.
