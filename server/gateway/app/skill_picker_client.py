"""Upgrade the skill menu in byte-verified workers retained across gateway releases."""
from functools import lru_cache

CATALOG = """  const nativeReviewCatalog=(id,force=false)=>skillState.load('native:'+id,()=>fetchNativeReviewCatalog(id),force);
  function pickerRows(id, finished=false) {
    const native = skillState.peek('native:'+id) || [];
    const projects = skillState.peek('projects:'+id) || [];
    const legacy = skillState.peek('legacy:'+id) || [];
    const seen = new Set();
    const rows = [];
    for (const row of [...native, ...projects, ...legacy]) {
      if (seen.has(row.plugin_key)) continue;
      if (!row.nativeSkill && (Object.hasOwn(nativeReviewNames,row.plugin_key) || row.plugin_key === 'employee-badges')) continue;
      seen.add(row.plugin_key);
      rows.push(row);
    }
    return rows.length || finished ? rows : null;
  }
  async function loadSkillCatalog(id, onProgress, force=false) {
    if (!id) return {plugins:[],error:''};
    const sources = [
      nativeReviewCatalog(id,force),
      skillState.load('projects:'+id, async () => {
        const value=await request('/__megmeet/tools/catalog?session_id='+encodeURIComponent(id),undefined,false,{timeoutMs:15000});
        return (value.items || []).map(p=>({...p,connector:true}));
      },force),
      skillState.load('legacy:'+id, async () => {
        const value=await request('/__megmeet/enterprise/plugins',undefined,false,{timeoutMs:15000});
        return value.items || [];
      },force)
    ];
    let failed=0;
    await Promise.all(sources.map(async (pending)=>{
      try { await pending; } catch { failed++; }
      const rows=pickerRows(id);
      if (rows && onProgress) onProgress(rows);
    }));
    return {plugins:pickerRows(id,true),
      error:failed===3?'技能暂时无法加载，请稍后重试。':failed?'部分技能暂时未加载，已显示可用内容。':''};
  }
"""

CHANGES = (
    ("const label=nativeReviewNames[row.name]||", "const label=(row.name==='company-employee-badges'?'工卡制作':nativeReviewNames[row.name])||"),
    ("useState(() => skillState.peek(catalogKey(sessionId))?.plugins || null)", "useState(() => pickerRows(sessionId))"),
    ("const value = await loadSkillCatalog(sessionId);",
     """const force=event?.type==='focus';
          if (force && live) { setPlugins(null); setError(''); }
          const value = await loadSkillCatalog(sessionId, rows => { if (live) setPlugins(rows); }, force);"""),
    ("const refresh = async () => {\n        if (pending) return; pending = true;",
     "const refresh = async (event) => {\n        if (pending) return; pending = true;"),
    ("const hot=skillState.peek(catalogKey(id))||await loadSkillCatalog(id);",
     "const hot=await nativeReviewCatalog(id);"),
    ("!hot?.plugins.some(row=>row.nativeSkill&&row.plugin_key===plugin.plugin_key)",
     "!hot.some(row=>row.nativeSkill&&row.plugin_key===plugin.plugin_key)"),
    ("const next=ids.filter(key=>!(enabled&&isWorkflow(key)));",
     """const next=ids.filter(key=>!(enabled&&isWorkflow(key)) && key!=='employee-badges');
    if (enabled && name==='company-employee-badges') next.push('employee-badges');"""),
    ("toolIds.filter(value=>value!==key),nativeReviewPrefix(before.draft)===key?",
     "toolIds.filter(value=>value!==key && !(key==='company-employee-badges' && value==='employee-badges')),nativeReviewPrefix(before.draft)===key?"),
    ("...toolIds.filter(key=>key!==nativeSelected).map(",
     "...toolIds.filter(key=>key!==nativeSelected && !(nativeSelected==='company-employee-badges' && key==='employee-badges')).map("),
    ("const isWorkflow = key => Object.hasOwn(workflowNames, key);",
     "const isWorkflow = key => key==='employee-badges' || Object.hasOwn(workflowNames, key);"),
    ("const clearNative=isWorkflow(plugin.plugin_key)&&next.includes(plugin.plugin_key)&&prior;",
     """const clearNative=isWorkflow(plugin.plugin_key)&&next.includes(plugin.plugin_key)&&prior;
            if(clearNative && prior==='company-employee-badges') next=next.filter(key=>key!=='employee-badges');"""),
)

def adapt_picker(text, *, strict=False):
    """Apply only the known source anchors; older worker formats retain their own UI."""
    opening="  const catalogKey=id=>'catalog:'+id;"
    closing='  const toolReads=new Map(),toolReadVersions=new Map();'
    if text.count(opening) != 1 or text.count(closing) != 1 or any(text.count(old)!=1 for old, _ in CHANGES):
        if strict:
            raise ValueError('skill_picker_source_mismatch')
        return text
    start, end=text.index(opening),text.index(closing)
    if start>=end or "const values=await Promise.allSettled([" not in text[start:end]:
        if strict:
            raise ValueError('skill_picker_catalog_mismatch')
        return text
    updated=text[:start]+CATALOG+text[end:]
    for old,new in CHANGES:
        updated=updated.replace(old,new,1)
    return updated

@lru_cache(maxsize=2)
def skill_picker_client(company):
    source=company.decode('utf-8')
    updated=adapt_picker(source)
    return company if source is updated else updated.encode('utf-8')
