"""Run inside the sales-order service. Verify database metadata without calling business procedures."""
import json
import os
import sys

NAMES=("PARSE_XML_DATA","VALIDATE_DATA","CREATE_ORDER")

def main():
    mandatory=("ORACLE_DSN","ORACLE_USER","ORACLE_PASSWORD","ORACLE_ENVIRONMENT","ORACLE_EXPECTED_DSN")
    missing=[name for name in mandatory if not os.environ.get(name)]
    summary={"configured":not missing,"missing":missing,"write_enabled":os.getenv("ORDER_ENABLE_ORACLE_WRITE","false").lower()=="true",
             "business_procedures_called":False}
    if missing:
        print(json.dumps(summary))
        return 2
    from order_mcp.config import Settings
    from order_mcp.oracle import OracleGateway
    try:
        gateway=OracleGateway(Settings.from_env())
        summary["target"]=gateway.target_status()
        with gateway.connect() as conn,conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SELECT 1 FROM DUAL")
            summary["readonly_connection"]=cur.fetchone()==(1,)
            pkg="CUX_BLUE_AI_OE_ORDER_PKG"
            # Direct package takes precedence over a private synonym, then a public synonym.
            cur.execute("""SELECT owner, object_name FROM (
                    SELECT 0 priority,owner,object_name FROM all_objects
                        WHERE owner=SYS_CONTEXT('USERENV','CURRENT_SCHEMA') AND object_name=:pkg AND object_type='PACKAGE'
                    UNION ALL
                    SELECT CASE WHEN owner='PUBLIC' THEN 2 ELSE 1 END,table_owner,table_name FROM all_synonyms
                        WHERE synonym_name=:pkg AND owner IN (SYS_CONTEXT('USERENV','CURRENT_SCHEMA'),'PUBLIC') AND db_link IS NULL
                    ORDER BY 1) WHERE ROWNUM=1""",pkg=pkg)
            resolved=cur.fetchone()
            summary["package_resolved"]=bool(resolved)
            visible=set()
            if resolved:
                cur.execute("""SELECT DISTINCT procedure_name FROM all_procedures WHERE owner=:owner AND object_name=:pkg
                         AND procedure_name IN ('PARSE_XML_DATA','VALIDATE_DATA','CREATE_ORDER')""",
                         owner=resolved[0],pkg=resolved[1])
                visible={p[0] for p in cur}
            summary["visible_procedures"]={name:name in visible for name in NAMES}
            conn.rollback()
            ok=summary["readonly_connection"] and all(summary["visible_procedures"].values())
    except Exception as exc:
        summary["error_type"]=type(exc).__name__
        if exc.args and isinstance(getattr(exc.args[0],"code",None),int):
            summary["oracle_error_code"]=exc.args[0].code
        ok=False
    print(json.dumps(summary))
    return 0 if ok else 1

if __name__=="__main__":
    sys.exit(main())
