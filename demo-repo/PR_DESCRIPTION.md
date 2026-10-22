# Add discounts and bulk pricing

apply_discount takes a percent between 0 and 100. Negative quantities raise ValueError by design (refunds are handled elsewhere). cart_total applies a 5% bulk price when the average item price is over 100.
