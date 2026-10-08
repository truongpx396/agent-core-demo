"""Text from the database, made safe to print on an operator's terminal (constitution VI: untrusted content is data).

`scripts/credits.py show` and the reconciliation report print strings this app did not write: a payment reference a provider
chose (`credit_lots.external_ref`), a tenant name taken from an identity token, a reason stored by any code path. A terminal
obeys escape sequences, so a reference containing one could rewrite what the operator reads (hide a line, forge a balance).
Anything not printable is shown as its escape (`\\x1b`), which is visible and inert."""


def printable(value: object) -> str:
    text = "" if value is None else str(value)
    return text if text.isprintable() else text.encode("unicode_escape").decode("ascii")
