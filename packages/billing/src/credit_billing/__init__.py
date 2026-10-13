"""Credit billing: the app-owned wallet and, later, the seams a payment provider plugs into.

See specs/010-credit-billing-readiness/. The one rule that shapes everything here: the app owns the
real-time ENTITLEMENT (what a tenant may still consume) and the payment provider owns the MONEY,
because neither Stripe (credits settle on an invoice) nor Polar (it does not block usage past a
balance) can answer "may this request run now?".
"""
