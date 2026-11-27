"""Read-only ERP references for the interactive sales order review."""
import math
import re
import unicodedata
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from difflib import SequenceMatcher
from time import monotonic

from .rules import normalize_name

CUSTOMERS = """
select account_number,party_name from (
 select account_number,party_name from (
   select s.account_number,h.party_name,
          upper(replace(replace(replace(replace(h.party_name,'（','('),'）',')'),' ',''),'　','')) search_name
   from hz_parties h join hz_cust_accounts s on s.party_id=h.party_id
   where h.status='A' and s.status='A' {account_filter}
 ) where {match}
 order by case when search_name=:full_name then 0 else 1 end,
          {overlap} desc, length(search_name),account_number
) where rownum<=96
"""

ITEMS = """
select item_code,customer_item,unit from (
 select msi.segment1 item_code,mci.customer_item_number customer_item,msi.primary_uom_code unit,
        min(mcix.preference_number) preference
 from mtl_customer_items mci
 join mtl_customer_item_xrefs mcix on mcix.customer_item_id=mci.customer_item_id
 join mtl_system_items_b msi on msi.inventory_item_id=mcix.inventory_item_id and msi.organization_id=82
 join hz_cust_accounts cust on cust.cust_account_id=mci.customer_id
 join hz_parties party on party.party_id=cust.party_id
 where cust.account_number=:account and mci.customer_item_number=:customer_item
   and cust.status='A' and party.status='A' and nvl(mci.inactive_flag,'N')='N'
   and mcix.master_organization_id=82 and nvl(mcix.inactive_flag,'N')='N' and msi.enabled_flag='Y'
   and (msi.start_date_active is null or msi.start_date_active<=sysdate)
   and (msi.end_date_active is null or msi.end_date_active>=trunc(sysdate))
 group by msi.segment1,mci.customer_item_number,msi.primary_uom_code
 order by preference nulls last,item_code
) where rownum<=:max_rows
"""

ACTIVE_ITEM = """msi.organization_id=82 and msi.enabled_flag='Y'
 and (msi.start_date_active is null or msi.start_date_active<=sysdate)
 and (msi.end_date_active is null or msi.end_date_active>=trunc(sysdate))"""
INTERNAL_ITEM = f"select msi.segment1 from mtl_system_items_b msi where {ACTIVE_ITEM} and msi.segment1=:code"
MODEL_TOKEN_PREDICATE = """(instr(upper({field}),:model)>0 and regexp_like(upper({field}),:boundary,'c'))"""
CUSTOMER_MODELS = f"""
select distinct msi.segment1 from mtl_customer_items ci
 join hz_cust_accounts cust on cust.cust_account_id=ci.customer_id
 join mtl_customer_item_xrefs x on x.customer_item_id=ci.customer_item_id
 join mtl_system_items_b msi on msi.inventory_item_id=x.inventory_item_id
 where {ACTIVE_ITEM} and cust.account_number=:account and cust.status='A'
 and x.master_organization_id=82 and nvl(ci.inactive_flag,'N')='N' and nvl(x.inactive_flag,'N')='N'
 and ({MODEL_TOKEN_PREDICATE.format(field='ci.customer_item_desc')}
      or {MODEL_TOKEN_PREDICATE.format(field='msi.description')})
"""
INVENTORY_MODELS = f"""select distinct msi.segment1 from mtl_system_items_b msi
 where {ACTIVE_ITEM} and {MODEL_TOKEN_PREDICATE.format(field='msi.description')}"""

HISTORY = """
select unit_selling_price,booked_date,latest_pickup_date,order_quantity_uom,price_date from (
 select l.unit_selling_price,h.booked_date,d.latest_pickup_date,l.order_quantity_uom,
        coalesce(d.latest_pickup_date,h.booked_date,h.ordered_date,h.creation_date) price_date
 from hz_cust_accounts a join hz_parties p on p.party_id=a.party_id
 join oe_order_headers_all h on h.sold_to_org_id=a.cust_account_id
 join oe_order_lines_all l on l.header_id=h.header_id and l.org_id=h.org_id
 join mtl_system_items_b msi on msi.inventory_item_id=l.inventory_item_id and msi.organization_id=82
 outer apply (select max(w.latest_pickup_date) latest_pickup_date
              from wsh_delivery_details w where w.source_line_id=l.line_id
              and w.source_code='OE' and w.released_status not in ('R','D')) d
 where a.account_number=:account and p.party_name=:customer_name
   and exists (select 1 from xle_le_ou_ledger_v ou
               where ou.operating_unit_id=h.org_id and ou.legal_entity_name=:entity)
   and h.transactional_curr_code=:currency and msi.segment1=:item
   and l.ordered_quantity>0 and l.unit_selling_price>0 and nvl(l.cancelled_flag,'N')='N'
   and nvl(h.cancelled_flag,'N')='N' and nvl(l.line_category_code,'ORDER')='ORDER'
   and nvl(h.flow_status_code,'ENTERED')<>'CANCELLED' and nvl(l.flow_status_code,'ENTERED')<>'CANCELLED'
   and (:exclude_po is null or h.cust_po_number is null or h.cust_po_number<>:exclude_po)
   and (:unit is null or l.order_quantity_uom=:unit)
 order by price_date desc nulls last,h.header_id desc,l.line_id desc
) where rownum=1
"""

USED_POS = """
with customer as (select cust_account_id from hz_cust_accounts where account_number=:account)
select distinct h.cust_po_number,0 from customer s join oe_order_headers_all h
 on h.sold_to_org_id=s.cust_account_id where h.cust_po_number like :prefix
union all
select null,count(h.header_id) from customer s join oe_order_headers_all h on h.sold_to_org_id=s.cust_account_id
 where h.creation_date>=to_date(:day,'YYYYMMDD') and h.creation_date<to_date(:day,'YYYYMMDD')+1
 and nvl(h.cancelled_flag,'N')<>'Y' and nvl(h.flow_status_code,'ENTERED')<>'CANCELLED'
 and (h.order_category_code='ORDER' or (nvl(h.order_category_code,'MIXED')='MIXED' and exists
     (select 1 from oe_order_lines_all l where l.header_id=h.header_id and l.line_category_code='ORDER'
      and l.ordered_quantity>0 and nvl(l.cancelled_flag,'N')<>'Y')))
"""

DOMESTIC_DEFAULTS = """
select distinct t.transaction_type_id,tl.name,mp.organization_code from oe_transaction_types_all t
 join oe_transaction_types_tl tl on tl.transaction_type_id=t.transaction_type_id
 left join mtl_parameters mp on mp.organization_id=t.warehouse_id
 where t.org_id=(select min(operating_unit_id) from xle_le_ou_ledger_v
                 where legal_entity_name=:entity having count(distinct operating_unit_id)=1)
 and t.transaction_type_code='ORDER' and tl.name like '%内销-出货订单'
 and (t.start_date_active is null or t.start_date_active<=sysdate)
 and (t.end_date_active is null or t.end_date_active>=trunc(sysdate))
"""

def _value(text, maximum=240):
    if not isinstance(text,str) or any(ord(char)<32 for char in text):
        return ''
    text=text.strip()
    return text if len(text)<=maximum else ''

def _stem(name):
    name=unicodedata.normalize('NFKC',name).casefold()
    compact=''.join(char for char in name if char.isalnum())
    return re.sub(r'(?:样例买方戊有限公司|有限责任公司|有限公司|有限合伙|公司)$','',compact) or compact

def _terms(stem):
    return {stem[i:i+size] for size in (1,2,3) for i in range(len(stem)-size+1)}

def _rank(query,candidates):
    # Rare character sequences carry more information than ubiquitous legal suffixes.
    # Fit weights on the local shortlist. No customer-directory data leaves the process.
    stem=_stem(query)
    sets=[_terms(_stem(row['name'])) for row in candidates]
    needle=_terms(stem)
    frequency=Counter(t for terms in sets for t in terms)
    def weight(term):
        return (1+math.log((len(sets)+1)/(frequency[term]+1)))**2 * len(term)
    def key(pair):
        row,terms=pair
        other=_stem(row['name'])
        same=normalize_name(row['name']).casefold()==normalize_name(query).casefold()
        common=sum(weight(t) for t in needle&terms)
        denom=math.sqrt(sum(weight(t) for t in needle)*sum(weight(t) for t in terms))
        cosine=common/denom if denom else 0
        score=.70*cosine+.30*SequenceMatcher(None,stem,other,autojunk=False).ratio()
        return (not same, stem!=other, -score, row['name'], row['account_number'])
    return [row for row,_ in sorted(zip(candidates,sets),key=key)]

def rerank_customers(query,candidates,ranker):
    """Optional employee-scoped model callback. Receives just query and indexed names.

    The callback returns a complete zero-based permutation. Protect exact matches,
    the directory membership, and the fallback order when the callback is unavailable.
    """
    if ranker is None or len(candidates)<2:
        return candidates
    if normalize_name(candidates[0]['name']).casefold()==normalize_name(query).casefold():
        return candidates
    try:
        indices=ranker(query,[{'index':i,'name':row['name']} for i,row in enumerate(candidates)])
    except Exception:
        return candidates
    if (not isinstance(indices,list) or any(type(i) is not int for i in indices)
            or sorted(indices)!=list(range(len(candidates)))):
        return candidates
    return [candidates[i] for i in indices]

class SalesReference:
    def __init__(self,oracle_gateway,*,ranker=None):
        self.gateway=oracle_gateway
        self.ranker=ranker
        self._session=None
        self._warehouses={}

    def warehouse_exists(self, code):
        last=self._warehouses.get(code)
        if last and monotonic()-last[0]<60:
            return last[1]
        rows=self._read("select count(*) from mtl_parameters where organization_code=:code", {"code":code})
        found=rows[0][0]==1
        self._warehouses[code]=(monotonic(),found)
        return found

    @contextmanager
    def session(self,*,timeout=12):
        """Use a single consistent Oracle snapshot and per-order memoization."""
        if self._session is not None:
            yield self
            return
        with self.gateway.connect() as conn,conn.cursor() as cur:
            conn.call_timeout=8000
            cur.execute('set transaction read only')
            scoped=SalesReference(self.gateway,ranker=self.ranker)
            scoped._session=(conn,cur,{},monotonic()+timeout)
            try:
                yield scoped
            finally:
                scoped._session=None

    def _read(self,sql,binds):
        if self._session is None:
            with self.session() as scope:
                return scope._read(sql,binds)
        conn,cur,cache,deadline=self._session
        key=(sql,tuple(sorted(binds.items())))
        if key in cache:
            return cache[key]
        ms=int((deadline-monotonic())*1000)
        if ms<1:
            raise TimeoutError('Oracle 参考资料查询超时。')
        conn.call_timeout=min(ms,8000)
        cur.execute(sql,binds)
        rows=cur.fetchall()
        cache[key]=rows
        return rows

    def customers(self,query,*,account_number=None,limit=8):
        query=_value(query)
        full=normalize_name(query).upper()
        stem=_stem(query).upper()
        if account_number is None and len(stem)<2:
            return []
        account_filter=''
        binds={'full_name':full}
        if account_number is not None:
            if not _value(account_number,80):
                return []
            account_filter='and s.account_number=:account'
            binds['account']=account_number.strip()
        # A partial or slightly mistyped name should still retrieve alternatives.
        grams=list(dict.fromkeys(stem[i:i+2] for i in range(len(stem)-1)))
        if len(grams)>10:
            grams=[grams[round(i*(len(grams)-1)/9)] for i in range(10)]
        pieces=[]
        for i,gram in enumerate(grams):
            binds['g'+str(i)]=gram
            pieces.append(f'case when instr(search_name,:g{i})>0 then 1 else 0 end')
        expression=' + '.join(pieces) or 'length(search_name)*0'
        match='1=1' if account_filter else 'search_name=:full_name or ('+expression+')>0'
        sql=CUSTOMERS.format(account_filter=account_filter,match=match,overlap='('+expression+')')
        rows=[{'name':str(name),'account_number':str(account)} for account,name in self._read(sql,binds)]
        short=_rank(query,rows)[:max(1,min(20,int(limit)))]
        return rerank_customers(query,short,self.ranker)

    def items(self,account_number,customer_item,business_entity=None,*,limit=8):
        account,item=_value(account_number,80),_value(customer_item)
        if not account or not item:
            return []
        rows=self._read(ITEMS,dict(account=account,customer_item=item,max_rows=max(1,min(20,int(limit)))))
        return [dict(item_code=str(code),customer_item=str(ref),unit=unit) for code,ref,unit in rows]

    def inventory_code(self, code):
        code = _value(code, 80)
        rows = self._read(INTERNAL_ITEM, {"code":code.upper()}) if code else []
        return str(rows[0][0]) if len(rows)==1 else None

    def items_by_model(self, account_number, model):
        model = _value(model, 120).upper()
        if len(model)<2 or not any(char.isalnum() for char in model):
            return []
        # Escape ERE metacharacters. The endpoints prevent matching a shorter variant of a model.
        literal = re.sub(r'([.\\^$*+?{}\[\]|()])',r'\\\1',model)
        boundary = '(^|[^A-Z0-9_-])' + literal + '($|[^A-Z0-9_-])'
        if len(boundary.encode("utf-8"))>500:
            return []
        binds = {"model":model, "boundary":boundary}
        account = _value(account_number,80)
        sources = [(CUSTOMER_MODELS, binds|{"account":account})] if account else []
        sources.append((INVENTORY_MODELS, binds))
        for sql, params in sources:
            rows = self._read("select * from ("+sql+" order by 1) where rownum<=9",params)
            if rows:
                return [str(row[0]) for row in rows]
        return []

    def domestic_defaults(self,business_entity):
        entity=_value(business_entity)
        if not entity:
            return None
        rows=self._read(DOMESTIC_DEFAULTS,dict(entity=entity))
        if len(rows)!=1 or not rows[0][2]:
            return None
        _,order_type,ship_org_code=rows[0]
        return dict(order_type=order_type,ship_org_code=ship_org_code)

    def price(self,account_number,customer_name,item_code,business_entity,currency,price_basis='unknown',*,unit=None,exclude_po=None):
        currency=_value(currency,12).upper()
        if currency in ('RMB','人民币'):
            currency='CNY'
        binds=dict(account=_value(account_number,80),customer_name=_value(customer_name),item=_value(item_code),
                   entity=_value(business_entity),currency=currency)
        if not all(binds.values()) or not re.fullmatch('[A-Z]{3}',currency):
            return None
        basis=self.gateway.settings.history_price_basis
        if basis not in ('inclusive','exclusive'):
            basis='unknown'
        if price_basis not in ('unknown',None,basis):
            return None
        binds['exclude_po']=_value(exclude_po) or None
        binds['unit']=_value(unit,25).upper() if unit is not None else None
        if unit is not None and not binds['unit']:
            return None
        rows=self._read(HISTORY,binds)
        if not rows:
            return None
        value,booked,pickup,uom,price_date=rows[0]
        return dict(price=str(value),price_basis=basis,currency=currency,unit=uom,
                    booked_date=booked.isoformat() if booked else None,latest_pickup_date=pickup.isoformat() if pickup else None,
                    price_date=price_date.isoformat() if price_date else None,
                    requires_confirmation=True)

    def used_po_sequences(self,account_number,date_prefix,*,separator='',business_entity=None):
        account=_value(account_number,80)
        if not account or not isinstance(date_prefix,str) or not re.fullmatch('[0-9]{8}',date_prefix) or separator:
            raise ValueError('须提供客户账号和八位采购日期。')
        datetime.strptime(date_prefix,'%Y%m%d')
        used=set()
        for value,today_orders in self._read(USED_POS,dict(account=account,prefix=date_prefix+'%',day=date_prefix)):
            if today_orders:
                used.add(int(today_orders))
            tail=str(value)[8:]
            if re.fullmatch('[0-9]{2,12}',tail) and int(tail)>0:
                used.add(int(tail))
        return used
