"""Strict receipt checks for the known empty-error-group defect in the test ERP."""
from hashlib import sha256
import re
import os


TEST_TARGET = {"host": "192.0.2.45", "port": 1541, "service_name": "EXAMPLE_TEST"}
TEST_TARGET = {
    "host": os.getenv("ORDER_VERIFIED_RECEIPT_TEST_HOST", TEST_TARGET["host"]).lower(),
    "port": int(os.getenv("ORDER_VERIFIED_RECEIPT_TEST_PORT", str(TEST_TARGET["port"]))),
    "service_name": os.getenv("ORDER_VERIFIED_RECEIPT_TEST_SERVICE", TEST_TARGET["service_name"]),
}
WRAPPER = "CUX_BLUE_AI_OE_ORDER_PKG"
VALIDATOR = "CUX_OM_MANY_PO_INSERT_SALE_PKG"
BODY_DIGESTS = {
    WRAPPER: "4d74d7eed123976abb51d8bface895fcb8ee466a9a3f7e89c21a706f062b36a1",
    VALIDATOR: "a3b1ad01c766e8ecf928641ef897e28aadb5edf54fcbdd43381426a23dae4e2d",
}
# Removing only BOOK_ORDER from the current wrapper reproduces this entire earlier body.
WRAPPER_BEFORE_BOOK_ORDER = "f004805f2727b4cde88c777341c396543554d0122302cdf7aae489d2be752803"
SUCCESS_TEXT_SHA = "5f78a82313bbf35d2b040a91bb250f7db5a656e189ad1c653701edc1edbafe5a"
FIRST_FRAME = re.compile(r'^ORA-06512\s*[:：]\s*[^"\n]*"APPS\.CUX_BLUE_AI_OE_ORDER_PKG"[^"\d\n]*163[^"\d\n]*$')

# Catch within the original statement. An unhandled PL/SQL exception rolls back
# statement-local changes, including the success receipt on each validated line.
VALIDATE = """
begin
  :backtrace := null;
  begin
    apps.cux_blue_ai_oe_order_pkg.validate_data(:batch, :status, :message);
  exception when no_data_found then
    :backtrace := dbms_utility.format_error_backtrace;
    :status := 'E';
    :message := null;
  end;
end;"""

# Both multiset counts and all underlying business keys must match the XML that
# was just parsed. SQL returns counts only. Any additional or missing row fails.
RECEIPT = """
with expected as (
  select x.ou, x.account, x.po, x.otype, x.ship, x.rel, x.item, x.qty,
         x.price, x.due, count(*) copies
  from xmltable('/ROOT/G_HEADER' passing xmltype(:document_xml)
       columns ou varchar2(4000) path 'OU_NAME', account varchar2(4000) path 'ACCOUNT_NUMBER',
       po varchar2(4000) path 'CUST_PO_NUMBER', otype varchar2(4000) path 'ORDER_TYPE',
       ship varchar2(4000) path 'SHIP_ORG_CODE', rel varchar2(4000) path 'RELEASE_ORG_CODE',
       item varchar2(4000) path 'SEGMENT1', qty number path 'ORDER_QUANTITY',
       price number path 'UNIT_SELLING_PRICE', due varchar2(10) path 'REQUEST_DATE') x
  group by x.ou, x.account, x.po, x.otype, x.ship, x.rel, x.item, x.qty, x.price, x.due
), batch_rows as (
  select t.* from cux.cux_om_sale_many_po_t t where t.batch_number=:batch
), verified as (
  select ou_name, account_number, cust_po_number, order_type, ship_org_code, release_org_code,
         segment1, order_quantity, unit_selling_price, to_char(request_date,'YYYY-MM-DD'), count(*)
  from batch_rows
  where import_status='N' and running_status='N' and order_number is null
    and import_message=:success_marker
    and org_id is not null and sold_to_org_id is not null and invoice_to_org_id is not null
    and ship_to_org_id is not null and order_type_id is not null and inventory_item_id is not null
    and price_list_id is not null and order_quantity > 0 and unit_selling_price > 0
    and created_by=(select user_id from apps.fnd_user where user_name=:employee
                    and nvl(start_date,sysdate-1)<=sysdate and nvl(end_date,sysdate+1)>sysdate)
  group by ou_name,account_number,cust_po_number,order_type,ship_org_code,release_org_code,
           segment1,order_quantity,unit_selling_price,to_char(request_date,'YYYY-MM-DD')
), missing as (select * from expected minus select * from verified)
select (select count(*) from batch_rows), (select sum(copies) from expected),
       (select count(distinct po) from expected), (select count(*) from missing),
       (select count(distinct org_id) from batch_rows), (select count(distinct invoice_to_org_id) from batch_rows),
       (select count(distinct sold_to_org_id) from batch_rows)
from dual"""


def is_test_target(target):
    return (target.get("environment") == "test" and target.get("target_matches") is True
            and target.get("expected_target") == TEST_TARGET)


class UnverifiedReceipt(ValueError):
    """Only caller-defined diagnostics may be shown on the order review card."""


def require(condition, diagnostic):
    if not condition:
        raise UnverifiedReceipt(diagnostic)


def require_test_service(cur):
    cur.execute("select lower(sys_context('USERENV','SERVICE_NAME')) from dual")
    require(cur.fetchone() == (TEST_TARGET["service_name"].lower(),), "连接的数据库服务与销售订单测试库配置不符")


def missing_item_counts(cur, batch, document_xml, employee):
    """Describe a definite rejection using counts only, bound to this upload and employee."""
    cur.execute("""
        with expected as (
          select x.ou,x.account,x.po,x.item from xmltable('/ROOT/G_HEADER' passing xmltype(:document_xml)
            columns ou varchar2(4000) path 'OU_NAME', account varchar2(4000) path 'ACCOUNT_NUMBER',
                    po varchar2(4000) path 'CUST_PO_NUMBER', item varchar2(4000) path 'SEGMENT1') x
        ), batch_rows as (
          select t.* from cux.cux_om_sale_many_po_t t where t.batch_number=:batch
        ), unmatched as (
          select ou,account,po,item,count(*) from expected group by ou,account,po,item
          minus
          select ou_name,account_number,cust_po_number,segment1,count(*) from batch_rows
           where created_by=(select user_id from apps.fnd_user where user_name=:employee)
           group by ou_name,account_number,cust_po_number,segment1
        )
        select (select count(*) from expected), (select count(*) from batch_rows),
               (select count(*) from unmatched),
               (select count(*) from batch_rows t where t.import_status='E' and t.inventory_item_id is null
                   and t.segment1 is not null and
                   (instr(t.import_message,'物料【'||t.segment1||'】不存在')>0 or
                    (t.ship_org_code is not null and instr(t.import_message,
                        '物料【'||t.segment1||'在组织'||t.ship_org_code||'】不存在')>0)))
        from dual""",
        document_xml=document_xml, employee=employee, batch=batch)
    expected, actual, unmatched, missing = cur.fetchone()
    if expected == actual and unmatched == 0 and 0 < missing <= actual:
        return missing, actual
    return None


def verified_success_marker(cur):
    for name, expected in BODY_DIGESTS.items():
        cur.execute("""select text from all_source where owner='APPS' and name=:name
                       and type='PACKAGE BODY' order by line""", name=name)
        body = "".join(row[0] for row in cur)
        actual = sha256(body.encode()).hexdigest()
        require(actual == expected or (name == WRAPPER and actual == WRAPPER_BEFORE_BOOK_ORDER),
                "测试库校验程序版本与已核验版本不一致，请 ERP 管理员核对版本")
        if name == VALIDATOR:
            marker = re.search(
                r"(?is)IF\s+x_err_count\s*=\s*0\s+THEN\s+UPDATE\s+cux\.cux_om_sale_many_po_t\s+t"
                r"\s+SET\s+import_status\s*=\s*'N'\s*,\s*import_message\s*=\s*'((?:''|[^'])+)'", body)
            require(marker is not None, "测试库校验程序缺少已核验的明细成功标记")
            value = marker[1].replace("''", "'")
            require(sha256(value.encode()).hexdigest() == SUCCESS_TEXT_SHA,
                    "测试库明细成功标记与已核验版本不一致")
    return value


def validate(cur, batch, status, message, document_xml, employee):
    trace = cur.var(str, 4000)
    cur.execute(VALIDATE, batch=batch, status=status, message=message, backtrace=trace)
    if not trace.getvalue():
        return False  # The official validator returned its normal S or E result.
    first_frame = trace.getvalue().lstrip().splitlines()[0]
    require(FIRST_FRAME.fullmatch(first_frame), "测试库校验过程返回未识别的异常位置，请 ERP 管理员核对")
    marker = verified_success_marker(cur)
    cur.execute(RECEIPT, batch=batch, document_xml=document_xml, employee=employee, success_marker=marker)
    actual, expected, distinct_po, missing, orgs, sites, customers = cur.fetchone()
    require(actual and actual == expected, "测试库批次记录数与提交的订单明细数不一致")
    require(distinct_po == orgs == sites == customers == 1,
            "测试库回执未确认同一采购单、业务实体、客户及开票地点")
    require(missing == 0, "测试库未能逐行确认提交数据与已校验明细一致")
    status.setvalue(0, "S")
    message.setvalue(0, "测试库完整业务校验已通过，所有明细与本次提交一致。")
    return True
