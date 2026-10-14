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


# --- executable(): the script as plain SQL (spec 012) --------------------------------------------------------------------

def test_executable_removes_exactly_the_connect_line_and_nothing_else():
    original = schema.read("20-credit-wallet.sql")

    runnable = schema.executable("20-credit-wallet.sql")

    assert "\\connect" not in runnable
    removed = [line for line in original.splitlines() if line not in runnable.splitlines()]
    assert removed == ["\\connect appdata"], "one line gone, and it is the psql meta-command"
    assert runnable.endswith("\n") and "CREATE TABLE IF NOT EXISTS credit_accounts" in runnable


@pytest.mark.parametrize("name", ["20-credit-wallet.sql", "22-billing.sql"])
def test_no_shipped_script_has_a_backslash_line_other_than_connect(name):
    """Else `executable()` would refuse it and the migration tool could not apply it."""
    offenders = [line for line in schema.read(name).splitlines() if line.strip().startswith("\\") and not line.startswith("\\connect")]

    assert offenders == []


def test_executable_refuses_any_other_psql_meta_command_naming_script_and_line(monkeypatch, tmp_path):
    (tmp_path / "30-demo.sql").write_text("\\connect appdata\nSELECT 1;\n  \\set x 1\nSELECT 2;\n")
    monkeypatch.setattr(schema, "_SQL", tmp_path)

    with pytest.raises(ValueError, match=r"30-demo\.sql, line 3: .*\\set x 1.*psql meta-command"):
        schema.executable("30-demo.sql")


def test_a_backslash_that_is_not_the_first_character_of_a_line_is_left_alone(monkeypatch, tmp_path):
    (tmp_path / "30-demo.sql").write_text("\\connect appdata\nSELECT E'a\\nb';\nCREATE FUNCTION f() RETURNS text AS $$ SELECT '\\d' $$ LANGUAGE sql;\n")
    monkeypatch.setattr(schema, "_SQL", tmp_path)

    runnable = schema.executable("30-demo.sql")

    assert "E'a\\nb'" in runnable and "'\\d'" in runnable


def test_connect_with_a_different_database_name_is_also_removed(monkeypatch, tmp_path):
    (tmp_path / "30-demo.sql").write_text("\\connect some_other_db\nSELECT 1;\n")
    monkeypatch.setattr(schema, "_SQL", tmp_path)

    assert schema.executable("30-demo.sql") == "SELECT 1;\n"


def test_scripts_are_ordered_by_their_number_as_an_integer_not_as_text(monkeypatch, tmp_path):
    for name in ("100-late.sql", "22-b.sql", "9-early.sql", "20-a.sql"):
        (tmp_path / name).write_text("SELECT 1;\n")
    monkeypatch.setattr(schema, "_SQL", tmp_path)

    assert schema.scripts() == ["9-early.sql", "20-a.sql", "22-b.sql", "100-late.sql"]


@pytest.mark.parametrize("bad", ["notes.sql", "x-20.sql", "20_underscore.sql", "20-.sql"])
def test_a_shipped_name_that_is_not_number_dash_words_is_refused_not_silently_misordered(monkeypatch, tmp_path, bad):
    (tmp_path / bad).write_text("SELECT 1;\n")
    monkeypatch.setattr(schema, "_SQL", tmp_path)

    with pytest.raises(ValueError, match="not a shipped script name"):
        schema.scripts()


def test_read_bytes_is_the_files_bytes_so_a_checksum_matches_sha256sum():
    assert schema.read_bytes("22-billing.sql") == schema.read("22-billing.sql").encode("utf-8")
