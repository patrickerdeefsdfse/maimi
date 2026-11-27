"""Stage one message's extracted files and commit its review drafts atomically."""
import json
import re
import uuid
from copy import deepcopy
from collections import defaultdict
from .service import apply_business_defaults
from .store import digest


def batch_key(value):
    if not isinstance(value,str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",value):
        raise ValueError("销售订单识别批次无效。")
    return value


def slot(index,count):
    if type(index) is not int or type(count) is not int or not 0<=index<count<=20:
        raise ValueError("每批可选择 1～20 份文件。")


def init(db):
    db.execute("""CREATE TABLE IF NOT EXISTS order_batch_inputs (
        employee TEXT NOT NULL, conversation TEXT NOT NULL, batch TEXT NOT NULL, idx INTEGER NOT NULL,
        total INTEGER NOT NULL, source_key TEXT NOT NULL, fragments TEXT NOT NULL,
        PRIMARY KEY(employee,conversation,batch,idx))""")
    db.execute("""CREATE TABLE IF NOT EXISTS order_batch_receipts (
        employee TEXT NOT NULL, conversation TEXT NOT NULL, batch TEXT NOT NULL, receipt TEXT NOT NULL,
        PRIMARY KEY(employee,conversation,batch))""")


def accept(store,batch,index,count,source_key,drafts=None):
    """A slot is immutable once saved. A resumed request may safely read it again."""
    slot(index,count)
    scope=(*store.scope,batch_key(batch))
    with store.connect() as db:
        init(db)
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT total,source_key FROM order_batch_inputs WHERE employee=? AND conversation=? AND batch=? AND idx=?",(*scope,index)).fetchone()
        if row:
            if row!=(count,source_key):
                raise ValueError("当前批次同一位置的原件发生变化，请重新发送。")
            return True
        if db.execute("SELECT 1 FROM order_batch_receipts WHERE employee=? AND conversation=? AND batch=?",scope).fetchone():
            raise ValueError("此批次已完成复核表生成，请重新发送新增文件。")
        totals=db.execute("SELECT DISTINCT total FROM order_batch_inputs WHERE employee=? AND conversation=? AND batch=?",scope).fetchall()
        if totals and totals!=[(count,)]:
            raise ValueError("当前批次的文件数量发生变化。")
        if drafts is not None:
            db.execute("INSERT INTO order_batch_inputs VALUES(?,?,?,?,?,?,?)",(*scope,index,count,source_key,json.dumps(drafts,ensure_ascii=False)))
        return False


def merge_inputs(entries):
    entries=list(entries)
    identity=("account_number","business_entity")
    profiles=defaultdict(set)
    for _,drafts in entries:
        for fragment in drafts:
            order=fragment["order"]
            key=(order.get("customer_po"),order.get("customer_name"))
            if all(key):
                profiles[key].add(tuple(order.get(f) for f in identity))
    def conflicting(values):
        return any(len({v for v in column if v})>1 for column in zip(*values))
    split_keys={key for key,values in profiles.items() if conflicting(values)}
    ambiguous_profiles=set()
    for key in split_keys:
        for profile in profiles[key]:
            if all(profile):
                continue
            compatible=[other for other in profiles[key] if all(not(a and b and a!=b) for a,b in zip(profile,other))]
            if conflicting(compatible):
                ambiguous_profiles.add((key,profile))
    result=[]
    groups=defaultdict(list)
    for pos,drafts in entries:
        for fragment in drafts:
            original={k:fragment[k] for k in ("file_name","sha256","original_path")}
            order=fragment["order"]
            key=(order.get("customer_po"),order.get("customer_name"))
            row_identity=(fragment["sha256"],tuple(fragment["sources"]))
            profile=tuple(order.get(f) for f in identity)
            ambiguous=(key,profile) in ambiguous_profiles
            candidates=[r for r in groups[key] if ((not ambiguous and not r["_ambiguous"]) or row_identity in r["_rows"])
                        and all(not (r["order"].get(f) and order.get(f) and r["order"][f]!=order[f]) for f in identity)] if all(key) else []
            matches=[r for r in candidates if row_identity in r["_rows"]]
            target=matches[0] if matches else candidates[0] if len(candidates)==1 else None
            overflow=any(r.get("segmented_po") for r in candidates)
            if target and row_identity not in target["_rows"] and len(target["order"]["lines"])+len(order["lines"])>500:
                target["segmented_po"]=key[0]
                overflow=True
                target=None
            if not target:
                target=deepcopy(fragment)
                target.update(originals=[original],header_conflicts=deepcopy(fragment.get("header_conflicts",{})),_rows={row_identity},_file_slots={pos},_ambiguous=ambiguous)
                if key in split_keys:
                    target["warnings"].append("同名客户的同一采购单号出现不同客户代号或公司主体，已分别列出，请核对各原件归属。")
                if ambiguous:
                    target["warnings"].append("此原件缺少可区分订单归属的信息，已单独保留，请核对后补全。")
                if overflow:
                    target["segmented_po"]=key[0]
                # Each row keeps its source even after multiple files are combined.
                target["sources"]=[{"file_name":original["file_name"],"row":n} for n in fragment["sources"]]
                groups[key].append(target)
                result.append(target)
                continue
            if pos not in target["_file_slots"]:
                target["originals"].append(original)
                target["_file_slots"].add(pos)
            if row_identity in target["_rows"]:
                continue
            target["_rows"].add(row_identity)
            merged=target["order"]
            for field, choices in fragment.get("header_conflicts",{}).items():
                values=[*(target["header_conflicts"].get(field) or []),merged.get(field),*choices]
                target["header_conflicts"][field]=list(dict.fromkeys(v for v in values if v))
                merged[field]=None
            if fragment.get("explicit_currency"):
                target["explicit_currency"]=True
            if fragment.get("language_currency") and not target.get("language_currency"):
                target["language_currency"]=fragment["language_currency"]
            for field,value in order.items():
                if field in {"lines","evidence"} or value is None or value=="":
                    continue
                conflict=target["header_conflicts"].get(field)
                if conflict is not None:
                    if str(value) not in conflict:
                        conflict.append(str(value))
                elif merged.get(field) in ("",None):
                    merged[field]=value
                    if field in fragment["provenance"]:
                        target["provenance"][field]=fragment["provenance"][field]
                elif merged[field]!=value:
                    target["header_conflicts"][field]=[str(merged[field]),str(value)]
                    merged[field]=None
                    target["provenance"].pop(field,None)
            offset=len(merged["lines"])
            existing={digest({k:v for k,v in line.items() if k!="source_line"}) for line in merged["lines"]}
            if any(digest({k:v for k,v in line.items() if k!="source_line"}) in existing for line in order["lines"]):
                target["warnings"].append("不同原件含有相同明细，已分别保留；请核对是否为重复行。")
            merged["lines"].extend(order["lines"])
            for path,value in order.get("evidence",{}).items():
                match=re.fullmatch(r"lines\.(\d+)\.(.+)",path)
                if match:
                    merged.setdefault("evidence",{})[f"lines.{offset+int(match[1])}.{match[2]}"]=value
            for path,value in fragment["provenance"].items():
                match=re.fullmatch(r"lines\.(\d+)\.(.+)",path)
                if match:
                    target["provenance"][f"lines.{offset+int(match[1])}.{match[2]}"]=value
            target["sources"].extend({"file_name":original["file_name"],"row":n} for n in fragment["sources"])
            target["warnings"].extend(fragment["warnings"])
    for draft in result:
        del draft["_rows"],draft["_file_slots"],draft["_ambiguous"]
        draft["warnings"]=list(dict.fromkeys(draft["warnings"]))
        for line,source in zip(draft["order"]["lines"],draft["sources"]):
            if source:
                line["source_line"]=f"{source['file_name']} · {source['row']}"
        apply_business_defaults(draft)
    return result


def finish(store,batch,skipped):
    scope=(*store.scope,batch_key(batch))
    if not isinstance(skipped,list) or any(type(n) is not int or not 0<=n<20 for n in skipped) or len(set(skipped))!=len(skipped):
        raise ValueError("无法核对识别失败的附件。")
    with store.connect() as db:
        init(db)
        db.execute("BEGIN IMMEDIATE")
        receipt=db.execute("SELECT receipt FROM order_batch_receipts WHERE employee=? AND conversation=? AND batch=?",scope).fetchone()
        if receipt:
            return json.loads(receipt[0])
        rows=db.execute("SELECT idx,total,fragments FROM order_batch_inputs WHERE employee=? AND conversation=? AND batch=? ORDER BY idx",scope).fetchall()
        if not rows:
            if skipped and sorted(skipped)==list(range(len(skipped))):
                return {"order_ids":[],"missing_indices":sorted(skipped)}
            raise ValueError("尚未收到本批次的附件。")
        missing=set(range(rows[0][1]))-{r[0] for r in rows}
        if not missing.issubset(skipped) or any(n>=rows[0][1] for n in skipped):
            raise ValueError("本批次还有附件待识别。")
        ids=[]
        for record in merge_inputs((idx,json.loads(fragments)) for idx,_,fragments in rows):
            rid=uuid.uuid4().hex
            record.update(id=rid,revision=1)
            db.execute("INSERT INTO records VALUES(?,?,?)",(rid,1,json.dumps(record,ensure_ascii=False)))
            db.execute("INSERT INTO scopes(id,employee,conversation) VALUES(?,?,?)",(rid,*store.scope))
            ids.append(rid)
        value={"order_ids":ids,"missing_indices":sorted(missing)}
        db.execute("INSERT INTO order_batch_receipts VALUES(?,?,?,?)",(*scope,json.dumps(value)))
        # Retain compact receipts for retries; review drafts now own all source information.
        db.execute("UPDATE order_batch_inputs SET fragments='[]' WHERE employee=? AND conversation=? AND batch=?",scope)
        return value
