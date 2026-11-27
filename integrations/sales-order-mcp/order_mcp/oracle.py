"""Fixed, parameterized Oracle operations; never execute model-generated SQL."""
from decimal import Decimal
from threading import Lock
import re
from . import oracle_test_validation
from .config import Settings
from .store import digest

_CLIENT_INIT_LOCK = Lock()


def business_error_detail(message):
    text = " ".join(str(message or "").split())[:3000]
    # Procedure output can include SQL, connection descriptors or untrusted order text.
    phrases = re.findall(
        r"(客户(?:名称|编码|代号|账号|账户|地址)?|收货(?:方|地址)|业务实体|币种|"
        r"订单类型|物料(?:编号|编码)?|订购项目|单价|数量|交货日期|价目表|"
        r"发运组织|发货组织|库存组织|税率|采购单号|订单号)"
        r"(?:\s*[:：]?\s*[A-Za-z0-9_-]{1,40})?\s*(不能为空|不能为零|不能为负数|"
        r"必须大于零|必须大于0|不存在|未配置|不匹配|未找到|未维护|无效|有误|重复|已存在|为空|错误)", text)
    return "，".join(dict.fromkeys(field + reason for field,reason in phrases))[:180]


class OraclePrecreateRejected(ValueError):
    """A confirmed non-creation; only raised before entering create_order."""

    def __init__(self, stage, message=None, *, missing_items=None):
        labels = {"parse":"Oracle 解析订单失败","validate":"Oracle 校验订单失败","connection":"Oracle 测试或业务库连接未就绪"}
        detail = str(message) if isinstance(message, oracle_test_validation.UnverifiedReceipt) else business_error_detail(message)
        if missing_items:
            missing, total = missing_items
            rows = f"全部 {total} 行" if missing == total else f"{total} 行中的 {missing} 行"
            item_issue = f"{rows}的物料编码在 ERP 测试库校验时未找到。请核对 ERP 物料编码，如编码正确，请联系主数据管理员核对测试库。"
            detail = (detail + "。" if detail else "") + item_issue
        super().__init__(labels[stage] + ("，" + detail if detail else "，请核对配置或批次回执。"))


class OracleCreateUncertain(ValueError):
    def __init__(self, message=None):
        detail = business_error_detail(message)
        super().__init__("Oracle 建单未返回有效成功回执。" + (detail + "。" if detail else ""))

PRICE_SQL = """SELECT * FROM (SELECT hp.party_name, ool.ordered_item,
 ool.unit_selling_price, ooh.booked_date
 FROM oe_order_headers_all ooh JOIN oe_order_lines_all ool ON ooh.header_id=ool.header_id
 JOIN hz_cust_accounts hca ON ooh.sold_to_org_id=hca.cust_account_id
 JOIN hz_parties hp ON hca.party_id=hp.party_id
 WHERE ool.ordered_quantity>0 AND hca.account_number=:account_number
 AND hp.party_name=:customer_name AND ool.ordered_item=:item_code
 AND EXISTS (SELECT 1 FROM wsh_delivery_details wdd WHERE wdd.source_line_id=ool.line_id
 AND wdd.released_status IN ('Y','C'))
 ORDER BY ooh.booked_date DESC NULLS LAST, ooh.header_id DESC, ool.line_id DESC) WHERE ROWNUM=1"""

class OracleGateway:
    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def target(dsn):
        import oracledb
        params = oracledb.ConnectParams()
        params.parse_connect_string(dsn)
        if not isinstance(params.host, str) or not isinstance(params.port, int) or not isinstance(params.service_name, str):
            raise ValueError("Oracle 目标须配置明确的单个地址、端口和服务名。")
        return {"host": params.host.lower(), "port": params.port, "service_name": params.service_name}

    def target_status(self):
        settings = self.settings
        missing = [name for name, value in (("ORACLE_DSN", settings.dsn), ("ORACLE_USER", settings.db_user),
            ("ORACLE_PASSWORD", settings.db_password), ("ORACLE_EXPECTED_DSN", settings.oracle_expected_dsn)) if not value]
        environment = settings.oracle_environment
        if environment not in {"test", "production"}:
            missing.append("ORACLE_ENVIRONMENT")
        if settings.oracle_driver_mode not in {"thin", "thick"}:
            missing.append("ORACLE_DRIVER_MODE")
        expected, matches = None, False
        try:
            if settings.oracle_expected_dsn:
                expected = self.target(settings.oracle_expected_dsn)
            if settings.dsn and expected:
                matches = self.target(settings.dsn) == expected
        except Exception:
            # Connection descriptors and driver errors may contain credentials.
            pass
        return {"environment": environment if environment in {"test", "production"} else "unconfigured",
                "expected_target": expected, "target_matches": matches, "missing_configuration": missing,
                "write_enabled": settings.enable_write,
                "driver_mode": settings.oracle_driver_mode if settings.oracle_driver_mode in {"thin", "thick"} else "unconfigured",
                "write_ready": settings.enable_write and not missing and matches,
                "multi_line_confirmed": settings.multi_line_confirmed}

    def initialize_driver(self):
        import oracledb
        # Thick mode is explicit. On Linux, the container supplies approved
        # Oracle Client libraries in the loader search path before startup.
        with _CLIENT_INIT_LOCK:
            if self.settings.oracle_driver_mode == "thick":
                if oracledb.is_thin_mode():
                    oracledb.init_oracle_client()
            elif not oracledb.is_thin_mode():
                raise ValueError("Oracle 驱动模式与服务配置不一致，请重新启动销售订单服务。")

    def connect(self):
        import oracledb
        s = self.settings
        if not s.dsn or not s.db_user or not s.db_password:
            raise ValueError("Oracle 连接尚未配置。")
        target = self.target_status()
        if target["missing_configuration"] or not target["target_matches"]:
            raise ValueError("Oracle 环境或目标尚未核对，请管理员核对环境、地址、端口和服务名。")
        self.initialize_driver()
        conn = oracledb.connect(user=s.db_user, password=s.db_password, dsn=s.dsn)
        conn.call_timeout = 60_000
        return conn

    def customer(self, name: str) -> list[dict]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT hca.account_number, hp.party_name FROM hz_cust_accounts hca
             JOIN hz_parties hp ON hp.party_id=hca.party_id WHERE hca.status='A'
             AND REPLACE(REPLACE(REPLACE(hp.party_name,'（','('),'）',')'),' ','')=:name""",
                        name=name.replace("（", "(").replace("）", ")").replace(" ", ""))
            return [{"account_number": str(a), "name": n} for a, n in cur.fetchmany(100)]

    def price(self, account_number: str, customer_name: str, item_code: str) -> dict | None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(PRICE_SQL, account_number=account_number, customer_name=customer_name, item_code=item_code)
            row = cur.fetchone()
            return {"customer_name": row[0], "item_code": row[1], "price": str(row[2]),
                    "booked_date": str(row[3]), "price_basis": self.settings.history_price_basis,
                    "requires_confirmation": True} if row else None

    @staticmethod
    def booking_source(target):
        return digest([target["environment"], target["expected_target"]])

    def created_receipt(self, batch, order, source):
        """Look only for positive evidence of one created header for this employee's batch."""
        if not isinstance(batch,str) or not re.fullmatch(r"[1-9][0-9]{0,37}",batch):
            return None
        target = self.target_status()
        reference = self.booking_source(target)
        if not target["target_matches"] or (source != reference if source else not oracle_test_validation.is_test_target(target)):
            return None
        with self.connect() as conn, conn.cursor() as cur:
            if oracle_test_validation.is_test_target(target):
                oracle_test_validation.require_test_service(cur)
            cur.execute("""SELECT count(*),count(CASE WHEN t.ou_name=:entity
                AND t.cust_po_number=:po AND t.account_number=:account
                AND t.created_by=(SELECT user_id FROM apps.fnd_user WHERE user_name=:employee)
                THEN h.header_id END),count(DISTINCT h.header_id),min(h.order_number)
                FROM cux.cux_om_sale_many_po_t t LEFT JOIN oe_order_headers_all h
                ON h.order_number=t.order_number AND h.org_id=t.org_id
                AND h.sold_to_org_id=t.sold_to_org_id AND h.cust_po_number=t.cust_po_number
                WHERE t.batch_number=:batch""",
                entity=order["business_entity"],po=order["customer_po"],account=order["account_number"],
                employee=self.settings.user_code,batch=int(batch))
            total, matching, headers, number = cur.fetchone()
        if (not total or total != len(order["lines"]) or matching != total or headers != 1
                or not re.fullmatch(r"[1-9][0-9]{0,37}",str(number))):
            return None
        return {"order_number":str(number),"batch_number":batch,"booking_source":reference,
                "validation_status":"S","creation_status":"S","evidence":"verified_created_header"}

    def book_order(self, batch_number: str, order_number: str, source=None) -> dict:
        """Run the documented fourth step for the batch returned by create_order."""
        if not isinstance(batch_number, str) or not re.fullmatch(r"[1-9][0-9]{0,37}", batch_number):
            raise ValueError("缺少有效的 Oracle 建单批次。")
        if not isinstance(order_number, str) or not re.fullmatch(r"[1-9][0-9]{0,37}", order_number):
            raise ValueError("缺少有效的 Oracle 建单回执。")
        target = self.target_status()
        if not target["write_ready"]:
            raise ValueError("Oracle 写入尚未就绪。")
        # All receipts produced before target stamps were introduced originated
        # on the confirmed ERP test instance. Never replay one against a clone.
        matches_source = (source == self.booking_source(target) if source is not None
                          else oracle_test_validation.is_test_target(target))
        if not matches_source:
            return {"status":"BATCH_REQUIRES_CHECK", "batch_number":batch_number}
        with self.connect() as conn, conn.cursor() as cur:
            if self.settings.oracle_environment == "test" and oracle_test_validation.is_test_target(target):
                oracle_test_validation.require_test_service(cur)
            # BOOK_ORDER acts on the entire batch. Verify that every line belongs
            # to the reviewed, still-unbooked header before entering the procedure.
            cur.execute("""SELECT count(*), count(CASE WHEN h.order_number=:number
                AND h.booked_flag='N' THEN 1 END), count(DISTINCT h.header_id)
                FROM cux.cux_om_sale_many_po_t t LEFT JOIN oe_order_headers_all h
                ON h.order_number=t.order_number AND h.org_id=t.org_id
                AND h.sold_to_org_id=t.sold_to_org_id AND h.cust_po_number=t.cust_po_number
                WHERE t.batch_number=:batch""",
                        number=int(order_number), batch=int(batch_number))
            total, matching, headers = cur.fetchone()
            if not total or total != matching or headers != 1:
                return {"status":"BATCH_REQUIRES_CHECK", "batch_number":batch_number}
            status, message = cur.var(str, 10), cur.var(str, 4000)
            cur.callproc("apps.cux_blue_ai_oe_order_pkg.book_order", keyword_parameters={
                "p_file_id": int(batch_number), "x_return_status": status, "x_return_message": message,
            })
            value = status.getvalue()
            if value == "S":
                conn.commit()
            else:
                conn.rollback()
            return {"status": value, "batch_number": batch_number}

    def submit(self, xml: str, checkpoint) -> dict:
        import oracledb
        s = self.settings
        if not s.enable_write or not s.user_code:
            raise ValueError("写入尚未启用或 ORACLE_USER_CODE 未配置。")
        try:
            connection = self.connect()
        except Exception:
            raise OraclePrecreateRejected("connection") from None
        with connection as conn, conn.cursor() as cur:
            known_test_erp = s.oracle_environment == "test" and oracle_test_validation.is_test_target(self.target_status())
            if known_test_erp:
                try:
                    oracle_test_validation.require_test_service(cur)
                except oracle_test_validation.UnverifiedReceipt as exc:
                    raise OraclePrecreateRejected("connection", exc) from None
                except Exception:
                    raise OraclePrecreateRejected("connection") from None
            status, message = cur.var(str, 10), cur.var(str, 3000)
            batch, number = cur.var(oracledb.DB_TYPE_NUMBER), cur.var(str, 4000)
            # write() opens and closes this temporary LOB itself; connection exit frees it.
            clob = conn.createlob(oracledb.DB_TYPE_CLOB)
            clob.write(xml)
            prefix = "cux_blue_ai_oe_order_pkg."
            stage = "parse"
            try:
                cur.callproc(prefix + "parse_xml_data", keyword_parameters={"p_user_code": s.user_code, "p_xml_data": clob,
                             "x_batch_number": batch, "x_return_status": status, "x_return_message": message})
                checkpoint({"stage": "parse", "batch_number": str(batch.getvalue()), "status": status.getvalue()})
                if status.getvalue() != "S":
                    raise OraclePrecreateRejected("parse", message.getvalue())
                stage = "validate"
                receipt = {"stage": "validate", "batch_number": str(batch.getvalue())}
                if known_test_erp:
                    recovered = oracle_test_validation.validate(cur, batch.getvalue(), status, message, clob, s.user_code)
                    if recovered:
                        receipt["validation_evidence"] = "test_erp_checked_all_lines"
                else:
                    cur.callproc(prefix + "validate_data", keyword_parameters={"p_batch_number": batch.getvalue(),
                                 "x_return_status": status, "x_return_message": message})
                checkpoint({**receipt, "status": status.getvalue()})
                if status.getvalue() != "S":
                    missing_items = None
                    if known_test_erp and status.getvalue() == "E":
                        try:
                            missing_items = oracle_test_validation.missing_item_counts(cur, batch.getvalue(), clob, s.user_code)
                        except oracledb.Error:
                            # An unavailable detail query must preserve the validator's own rejection.
                            pass
                    raise OraclePrecreateRejected("validate", message.getvalue(), missing_items=missing_items)
                checkpoint({"stage":"create","batch_number":str(batch.getvalue())})
                stage = "create"
                cur.callproc(prefix + "create_order", keyword_parameters={"p_batch_number": batch.getvalue(),
                             "x_out_order_number": number, "x_return_status": status, "x_return_message": message})
                if status.getvalue() != "S" or not number.getvalue():
                    raise OracleCreateUncertain(message.getvalue())
                conn.commit()
                return {"batch_number": str(batch.getvalue()), "order_number": str(number.getvalue()),
                        "booking_source":self.booking_source(self.target_status()),
                        "validation_status":"S","creation_status":"S","message":message.getvalue()}
            except Exception as exc:
                conn.rollback()
                if stage == "validate" and isinstance(exc, oracle_test_validation.UnverifiedReceipt):
                    raise OraclePrecreateRejected(stage, exc) from None
                if stage != "create" and not isinstance(exc,OraclePrecreateRejected):
                    raise OraclePrecreateRejected(stage) from None
                # The package may commit internally after create_order has started.
                raise
