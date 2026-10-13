"""The schema the package ships (spec 011 D20, D22): two idempotent scripts that travel inside the package.

What a script DOES against a real Postgres is proven by the integration tier (`pytest -m integration`), which seeds a container from
exactly these files. These hermetic tests pin what must be true for another project to use them: they are shipped, in order,
non-empty, safe to re-run, and declared as package data so an install actually carries them.
"""
import re
import tomllib
from pathlib import Path

import pytest

from credit_billing import schema

PACKAGE = Path(__file__).resolve().parents[1]


def test_the_package_ships_exactly_the_wallet_and_the_billing_scripts_in_order():
    assert schema.scripts() == ["20-credit-wallet.sql", "22-billing.sql"]


@pytest.mark.parametrize("name", ["20-credit-wallet.sql", "22-billing.sql"])
def test_each_script_is_non_empty_and_opens_with_the_psql_connect_it_documents(name):
    text = schema.read(name)

    assert len(text) > 1000
    assert "\\connect appdata" in text, "the scripts are written as init scripts for the `appdata` database (credit_billing/schema.py says so)"


@pytest.mark.parametrize("name", ["20-credit-wallet.sql", "22-billing.sql"])
def test_every_table_is_created_idempotently_so_an_operator_can_re_run_a_script(name):
    creates = re.findall(r"CREATE TABLE\s+(?!IF NOT EXISTS)", schema.read(name))

    assert creates == [], "a CREATE TABLE without IF NOT EXISTS fails the second time it is applied"


def test_the_wallet_script_creates_the_ledger_and_the_billing_script_the_inbox():
    wallet, billing = schema.read("20-credit-wallet.sql"), schema.read("22-billing.sql")

    for table in ("credit_accounts", "credit_lots", "credit_transactions", "credit_entries"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in wallet
    for table in ("billing_customers", "credit_products", "billing_webhook_events"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in billing


def test_the_scripts_are_declared_as_package_data_so_an_install_carries_them():
    data = tomllib.loads((PACKAGE / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["credit_billing"]

    assert "sql/*.sql" in data and "py.typed" in data
