"""Bind native badge results to authorized durable job and attachment reads."""
from functools import lru_cache

NATIVE_CALL = """  function nativeCardConnector(name,args) {
    if(name==='enterprise_project_call') return serviceArguments(args)?args.connector:null;
    if(name!=='employee_badges'||!args||typeof args!=='object'||Array.isArray(args))return null;
    const fileId=id=>typeof id==='string'&&/^pjf_[A-Za-z0-9_-]{32}$/.test(id);
    if(args.action==='template'&&Object.keys(args).length===1)return 'employee-badges';
    if(args.action!=='generate'||Object.keys(args).some(k=>!['action','attachment_id','allow_partial','photo_overrides'].includes(k)))return null;
    if('attachment_id' in args && !fileId(args.attachment_id))return null;
    if('allow_partial' in args && typeof args.allow_partial!=='boolean')return null;
    if('photo_overrides' in args && !(Array.isArray(args.photo_overrides)&&args.photo_overrides.length<=20&&args.photo_overrides.every(
      row=>row&&Object.keys(row).sort().join(',')==='attachment_id,employee_no'&&/^M[0-9]{8}$/.test(row.employee_no)&&fileId(row.attachment_id))))return null;
    return 'employee-badges';
  }
"""

EDITS = (
    ("  function serviceDispatch(event) {", NATIVE_CALL + "  function serviceDispatch(event) {"),
    ("||data?.name!=='enterprise_project_call'", "||!['enterprise_project_call','employee_badges'].includes(data?.name)"),
    ("!serviceArguments(data.arguments)", "!nativeCardConnector(data.name,data.arguments)"),
    ("cardResult(data.content,event.seq,data.arguments.connector)", "cardResult(data.content,event.seq,nativeCardConnector(data.name,data.arguments))"),
    ("event.data?.name!=='enterprise_project_call'", "!['enterprise_project_call','employee_badges'].includes(event.data?.name)"),
    ("const args=JSON.parse(raw);return serviceArguments(args)?", "const args=JSON.parse(raw),connector=nativeCardConnector(event.data.name,args);return connector?"),
    ("step:event.data.step,connector:args.connector,action:args.action", "step:event.data.step,connector,action:args.action"),
    ("location:context.state.service||context.state.salesOrder?", "location:context.state.service||context.state.salesOrder||context.state.expectedConnector==='employee-badges'?"),
    (":row.result.project_result.connector==='service')throw Error('PROJECT_ROOT_BINDING_INVALID')",
     ":['service','employee-badges'].includes(row.result.project_result.connector))throw Error('PROJECT_ROOT_BINDING_INVALID')"),
    ("&&keys.has(card.connector)&&opaque(card.id)", "&&(keys.has(card.connector)||card.connector==='employee-badges'&&/^badges-[a-f0-9]{32}$/.test(card.id))&&opaque(card.id)"),
    ("const context = value => { if (!sid.test(value.sessionId || '') || !keys.has(value.connector))",
     "const context = (value,badgeDownload=false) => { if (!sid.test(value.sessionId || '') || !(keys.has(value.connector)||badgeDownload&&value.connector==='employee-badges'))"),
    ("async download(value) { const session_id=context(value);", "async download(value) { const session_id=context(value,true);"),
)

def adapt_badge_results(text, *, strict=False):
    missing=[old for old,_ in EDITS if text.count(old)!=1]
    if missing:
        if strict:
            raise ValueError('badge_result_source_mismatch: '+str(missing))
        return text
    for old,new in EDITS:
        text=text.replace(old,new,1)
    return text

@lru_cache(maxsize=2)
def badge_result_client(company):
    source=company.decode('utf-8')
    updated=adapt_badge_results(source)
    return company if source is updated else updated.encode('utf-8')
