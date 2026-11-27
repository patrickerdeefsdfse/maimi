import pytest
from order_mcp import oracle_test_validation as compat


class Bind:
    def __init__(self, value=None):
        self.value = value

    def getvalue(self):
        return self.value

    def setvalue(self, index, value):
        self.value = value


class Cursor:
    def __init__(self, trace="", row=(2, 2, 1, 0, 1, 1, 1), normal_status="S"):
        self.trace, self.row, self.normal_status = trace, row, normal_status
        self.receipt_calls = 0

    def var(self, *args):
        return Bind()

    def execute(self, query, **args):
        if query == compat.VALIDATE:
            args["backtrace"].value = self.trace
            args["status"].value = "E" if self.trace else self.normal_status
        elif query == compat.RECEIPT:
            self.receipt_calls += 1
        else:
            raise AssertionError("unexpected SQL")

    def fetchone(self):
        return self.row


VALID = 'ORA-06512: at "APPS.CUX_BLUE_AI_OE_ORDER_PKG", line 163\nORA-06512: at line 5'


def check(cursor):
    status = Bind()
    recovered = compat.validate(cursor, 42, status, Bind(), "<ROOT/>", "SYNTHETIC")
    return recovered, status.getvalue()


@pytest.mark.parametrize("status", ["S", "E"])
def test_regular_or_repaired_package_does_not_enter_fallback(status):
    cursor = Cursor(normal_status=status)
    assert check(cursor) == (False, status)
    assert cursor.receipt_calls == 0


def test_exact_trace_and_full_receipt(monkeypatch):
    monkeypatch.setattr(compat, "verified_success_marker", lambda cur: "test-marker")
    cursor = Cursor(VALID)
    assert check(cursor) == (True, "S")
    assert cursor.receipt_calls == 1


@pytest.mark.parametrize("row", [
    (0,None,0,0,0,0,0), (1,2,1,1,1,1,1), (3,2,1,0,1,1,1),
    (2,2,2,0,1,1,1), (2,2,1,1,1,1,1), (2,2,1,0,1,2,1), (2,2,1,0,1,1,2),
])
def test_incomplete_or_mixed_receipts_do_not_validate(monkeypatch, row):
    monkeypatch.setattr(compat, "verified_success_marker", lambda cur: "test-marker")
    with pytest.raises(ValueError):
        check(Cursor(VALID, row))


@pytest.mark.parametrize("trace", [
    VALID.replace("163", "161"), VALID.replace("163", "1163"), VALID.replace("APPS.", "OTHER."),
    'ORA-06512: at "APPS.CUX_OM_MANY_PO_INSERT_SALE_PKG", line 222\n' + VALID,
])
def test_other_validation_exceptions_preserve_failure(trace):
    cursor = Cursor(trace)
    with pytest.raises(ValueError):
        check(cursor)
    assert cursor.receipt_calls == 0


def test_fixed_application_and_database_service_and_fingerprints():
    target = {"environment": "test", "target_matches": True, "expected_target": dict(compat.TEST_TARGET)}
    assert compat.is_test_target(target)
    assert not compat.is_test_target({**target, "environment":"production"})
    assert not compat.is_test_target({**target, "target_matches":False})
    assert not compat.is_test_target({**target, "expected_target": {**compat.TEST_TARGET, "host":"other"}})
    class Changed:
        def execute(self, *args, **kwargs):
            pass
        def fetchone(self):
            return ("different_service",)
        def __iter__(self):
            return iter([("recompiled different body",)])
    with pytest.raises(ValueError):
        compat.require_test_service(Changed())
    with pytest.raises(compat.UnverifiedReceipt, match="校验程序版本"):
        compat.verified_success_marker(Changed())


@pytest.mark.parametrize(("wrapper", "validator", "allowed"), [
    (compat.WRAPPER_BEFORE_BOOK_ORDER, compat.BODY_DIGESTS[compat.VALIDATOR], True),
    (compat.BODY_DIGESTS[compat.WRAPPER], compat.BODY_DIGESTS[compat.VALIDATOR], True),
    (compat.BODY_DIGESTS[compat.VALIDATOR], compat.BODY_DIGESTS[compat.VALIDATOR], False),
    (compat.BODY_DIGESTS[compat.WRAPPER], compat.WRAPPER_BEFORE_BOOK_ORDER, False),
])
def test_pinned_digests_are_specific_to_each_package(monkeypatch, wrapper, validator, allowed):
    from hashlib import sha256
    from types import SimpleNamespace

    wrapper_body = "synthetic wrapper"
    validator_body = ("IF x_err_count = 0 THEN UPDATE cux.cux_om_sale_many_po_t t "
                      "SET import_status='N', import_message='validated'")
    class Metadata:
        def execute(self, statement, *, name):
            self.body = {compat.WRAPPER: wrapper_body, compat.VALIDATOR: validator_body}[name]
        def __iter__(self):
            return iter([(self.body,)])
    digests = {wrapper_body.encode():wrapper, validator_body.encode():validator}
    monkeypatch.setattr(compat, "sha256", lambda data: SimpleNamespace(
        hexdigest=lambda: digests.get(data, sha256(data).hexdigest())))
    monkeypatch.setattr(compat, "SUCCESS_TEXT_SHA", sha256(b"validated").hexdigest())
    if allowed:
        assert compat.verified_success_marker(Metadata()) == "validated"
    else:
        with pytest.raises(compat.UnverifiedReceipt, match="校验程序版本"):
            compat.verified_success_marker(Metadata())


def test_unverified_test_receipt_reaches_review_without_creating_order(monkeypatch):
    from unittest.mock import MagicMock
    from types import SimpleNamespace
    from order_mcp.oracle import OracleGateway, OraclePrecreateRejected

    gateway = OracleGateway(SimpleNamespace(enable_write=True, oracle_environment="test", user_code="TEST"))
    connection = MagicMock()
    session = connection.__enter__.return_value
    cursor = session.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("example_test",)
    cursor.var.side_effect = [Bind("S"), Bind(), Bind(21), Bind()]
    monkeypatch.setattr(gateway, "connect", lambda: connection)
    monkeypatch.setattr(gateway, "target_status", lambda: dict(
        environment="test", target_matches=True, expected_target=compat.TEST_TARGET))
    def missing_row(*args):
        compat.require(False, "测试库批次记录数与提交的订单明细数不一致")
    monkeypatch.setattr(compat, "validate", missing_row)
    with pytest.raises(OraclePrecreateRejected, match="测试库批次记录数与提交的订单明细数不一致"):
        gateway.submit("<ROOT/>", lambda receipt: None)
    assert [call.args[0] for call in cursor.callproc.call_args_list] == ["cux_blue_ai_oe_order_pkg.parse_xml_data"]
    session.rollback.assert_called_once()
    session.commit.assert_not_called()


@pytest.mark.parametrize("counts, expected_detail", [
    ((4,4,0,4), "全部 4 行"), ((4,4,0,1), "4 行中的 1 行"),
    ((4,4,0,0), None), ((4,4,1,3), None), ((4,3,0,2), None), ((0,0,0,0), None),
])
def test_rejected_batch_only_describes_matching_item_receipts(monkeypatch, counts, expected_detail):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from order_mcp.oracle import OracleGateway, OraclePrecreateRejected

    gateway = OracleGateway(SimpleNamespace(enable_write=True, oracle_environment="test", user_code="TEST"))
    conn = MagicMock()
    session = conn.__enter__.return_value
    cursor = session.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [("example_test",), counts]
    cursor.var.side_effect = [Bind("S"), Bind("arbitrary internal text"), Bind(21), Bind()]
    monkeypatch.setattr(gateway, "connect", lambda: conn)
    monkeypatch.setattr(gateway, "target_status", lambda: dict(
        environment="test", target_matches=True, expected_target=compat.TEST_TARGET))
    def reject(cur, batch, status, message, xml, user):
        status.setvalue(0, "E")
        return False
    monkeypatch.setattr(compat, "validate", reject)
    with pytest.raises(OraclePrecreateRejected) as exc:
        gateway.submit("<ROOT/>", lambda receipt: None)
    sql = cursor.execute.call_args.args[0]
    assert "t.batch_number=:batch" in sql and "user_name=:employee" in sql
    assert "minus" in sql and "group by ou_name,account_number,cust_po_number,segment1" in sql
    assert "'物料【'||t.segment1||'】不存在'" in sql and "t.inventory_item_id is null" in sql
    if expected_detail:
        assert expected_detail in str(exc.value) and "主数据管理员" in str(exc.value)
    else:
        assert "主数据管理员" not in str(exc.value)
    assert "arbitrary internal text" not in str(exc.value)
    assert [call.args[0] for call in cursor.callproc.call_args_list] == ["cux_blue_ai_oe_order_pkg.parse_xml_data"]
    session.commit.assert_not_called()


@pytest.mark.parametrize(("environment","current","expected","flag","enabled"),[
    ("test","192.0.2.45:1541/EXAMPLE_TEST","192.0.2.45:1541/EXAMPLE_TEST","example_test",True),
    ("production","192.0.2.45:1541/EXAMPLE_TEST","192.0.2.45:1541/EXAMPLE_TEST","example_test",False),
    ("test","other:1541/other","other:1541/other","example_test",False),
    ("test","other:1541/other","192.0.2.45:1541/EXAMPLE_TEST","example_test",False),
    ("test","192.0.2.45:1541/EXAMPLE_TEST","192.0.2.45:1541/EXAMPLE_TEST","false",False),
    ("unconfigured","","","true",True),
])
def test_multiline_setting_is_scoped_when_test_token_is_used(monkeypatch, environment, current, expected, flag, enabled):
    from order_mcp.config import Settings
    for name,value in dict(ORACLE_ENVIRONMENT=environment, ORACLE_DSN=current, ORACLE_EXPECTED_DSN=expected,
                           ORACLE_MULTI_LINE_CONFIRMED="true" if flag == "true" else "false",
                           ORACLE_EBS_TEST_MULTI_LINE_CONFIRMED="true" if flag == "example_test" else "false").items():
        monkeypatch.setenv(name,value)
    assert Settings.from_env().multi_line_confirmed is enabled
