"""Upgrade the exact earlier company web module to the current sales editor. Generated from client.template.js."""
import hashlib

INPUT_SIZE = 1832526
INPUT_SHA = '223922ac4abcc6d3b3ae405f62b68ac2347902bd539aed9b8f8e586654ba004d'
OUTPUT_SHA = '94e261f8b5a10cc4e49d70c415860e89fa102d7ceb96bbf9a62b864eeb27c28a'
PATCH = (
    (1816198, 1816198, "    const canConfirm=actions.some(a=>a?.kind==='invoke'&&a.connector==='sales-orders'&&a.action==='confirm_order'&&a.arguments?.order_id===id&&a.arguments.revision===revision)\n      || (initialReview&&!submit);\n    const confirmOrder=()=>invoke('confirm_order',{order_id:id,revision,...(dirty?{changes:buildChanges()}:{})});\n"),
    (1816753, 1816753, "      editor.submission_error?h('p',{role:'alert',style:{color:'#883728'}},text(editor.submission_error,1000)):null,\n"),
    (1819280, 1819351, "        !readOnly&&canConfirm?btn(dirty?'保存更改并建单':'确认建单',confirmOrder,inactive,true):null,\n        !readOnly&&!canConfirm?btn('保存更正',save,inactive||!dirty,true):null,\n"),
    (1819505, 1819661, "        !readOnly&&!canConfirm&&prepare?btn('预览订单',()=>invoke('prepare',prepare.arguments),inactive||dirty):null,\n"),
    (1821400, 1821497, "      return !(editorIds.has(a?.arguments?.order_id)&&['prepare','submit','confirm_order'].includes(a?.action));\n"),
)

def _previous_order_client(data):
    if len(data) != INPUT_SIZE or hashlib.sha256(data).hexdigest() != INPUT_SHA:
        return data
    for start,end,text in reversed(PATCH):
        data = data[:start] + text.encode() + data[end:]
    if hashlib.sha256(data).hexdigest() != OUTPUT_SHA:
        raise ValueError("Order UI digest mismatch")
    return data

CUSTOMER_PATCH = (
    (1810104, 1810104, '    const [freeCompany,setFreeCompany]=useState(false);\n'),
    (1810149, 1810149, "    const companies=Array.isArray(editor.business_entities)?editor.business_entities:[];\n    const customers=Array.isArray(editor.customer_candidates)?editor.customer_candidates:[];\n    const customerReview=Array.isArray(editor.customer_candidates)||Array.isArray(editor.business_entities);\n    const customerChanged=customerReview&&['header.customer_name','header.account_number'].some(key=>own(changes,key));\n    const setCustomer=(name,account)=>{\n      setChanges(old=>{\n        const edited={...old};\n        for(const [field,value] of [['customer_name',name],['account_number',account]]){\n          const key='header.'+field;\n          if(value===plain(header[field]))delete edited[key];else edited[key]=value;\n        }\n        return edited;\n      });\n      setNotice('');\n    };\n"),
    (1812050, 1812155, "      const recheck=customerChanged&&row&&['item_code','unit_price'].includes(field);\n      const flag=recheck?'待核':modified?'已修改':(absent&&(required||issue.some(x=>x.code==='required')))?'待补':\n"),
    (1812465, 1812650, "      const description=[recheck?'更换客户后将重新核对料号和价格。':'',modified?'保存前：'+(base||'空'):'',rule?((rule.previous!=null?'原值：'+plain(rule.previous)+'。':'')+plain(rule.reason||'按业务规则填入')):'',\n"),
    (1813131, 1813670, "      const input=(extra={})=>h('input',{...props,type:'text',inputMode:numericFields.has(field)?'decimal':undefined,maxLength:500,autoComplete:'off',spellCheck:false,\n        placeholder:['request_date','order_date'].includes(field)?'YYYY-MM-DD':undefined,...extra});\n      const customCompany='__maimi_sales_custom__';\n      const companyOptions=companies.includes(value)||!value?companies:[value,...companies];\n      let controlNode=basisFields.has(field)?h('select',props,...[['','未填写'],['inclusive','含税'],['exclusive','未税'],['unknown','待确认']].map(([v,l])=>h('option',{key:v,value:v},l))):\n        input();\n      if(field==='business_entity'&&companies.length){\n        controlNode=h(React.Fragment,null,\n          h('select',{...props,value:freeCompany?customCompany:value,onChange:e=>{\n            const custom=e.target.value===customCompany;\n            setFreeCompany(custom);\n            if(!custom)bind(local,e.target.value,base);\n          }},h('option',{value:''},'请选择销售公司'),...companyOptions.map(v=>h('option',{key:v,value:v},v)),\n            h('option',{value:customCompany},'手动填写')),\n          freeCompany?input({'aria-label':'手动填写销售公司'}):null);\n      }else if(field==='customer_name'&&customerReview){\n        const account=own(changes,'header.account_number')?changes['header.account_number']:plain(header.account_number);\n        const matched=customers.findIndex(c=>c.party_name===value&&c.account_number===account);\n        controlNode=h('div',{style:{display:'flex',alignItems:'stretch',minWidth:0,gap:4}},\n          input({onChange:e=>setCustomer(e.target.value,e.target.value===plain(header.customer_name)?plain(header.account_number):'')}),\n          customers.length?h('select',{'aria-label':'选择客户候选',title:'选择客户名称及账号',disabled:inactive,value:'',\n            style:{...control,width:64,flex:'0 0 64px',padding:'3px 2px',cursor:inactive?'default':'pointer'},\n            onChange:e=>{const idx=Number(e.target.value);if(e.target.value!==''&&customers[idx])setCustomer(customers[idx].party_name,customers[idx].account_number);}},\n            h('option',{value:''},'候选'),...customers.map((c,i)=>h('option',{key:i,value:String(i)},(i===matched?'✓ ':'')+c.party_name+' · '+c.account_number))):null);\n      }\n      return h('div',{key:field,style:{display:'grid',gap:4,minWidth:0,alignContent:'start'}},\n"),
    (1819722, 1819835, "        !readOnly&&canConfirm?btn(customerChanged?'更新客户并重新核对':dirty?'保存更改并建单':'确认建单',customerChanged?save:confirmOrder,inactive,true):null,\n"),
)

def _customer_review_client(data):
    data = _previous_order_client(data)
    if len(data) != 1833076 or hashlib.sha256(data).hexdigest() != '94e261f8b5a10cc4e49d70c415860e89fa102d7ceb96bbf9a62b864eeb27c28a':
        return data
    for start, end, text in reversed(CUSTOMER_PATCH):
        data = data[:start] + text.encode() + data[end:]
    if hashlib.sha256(data).hexdigest() != 'd377a6ed5976995ccd08bf2298ffc7bd67f4a1a60522bbeed8563decc864d14b':
        raise ValueError("Customer review asset mismatch")
    return data


ORDER_LINE_LABELS = [("      h('details',null,h('summary',{style:{cursor:'pointer'}},'其他订单字段')", "      editor.ship_org_code?h('small',{style:tone},own(changes,'header.business_entity')?'更改销售公司后将重新匹配发运仓库。':'发运仓库 '+editor.ship_org_code):null,\n      h('details',null,h('summary',{style:{cursor:'pointer'}},'其他订单字段')"), ("['item_code','物料编码'],['customer_item','客户料号'],['quantity','数量']", "['customer_item','客户料号'],['specification','型号'],['item_code','物料编码'],['quantity','数量']"), ("['description','物料名称'],['specification','规格']", "['description','物料名称']"), ('const shown=extra?itemFields:itemFields.slice(0,7);', 'const shown=extra?itemFields:itemFields.slice(0,8);'), ("const props={'aria-label':name,value,disabled:inactive", "const props={'aria-label':name,'aria-required':required,value,disabled:inactive"), ('...shown.map(it=>it[1]),', "...shown.map(([key,label])=>key==='item_code'?label+'（必填）':label),"), ("['item_code','customer_item','request_date'].includes(k)", "['item_code','customer_item','specification','request_date'].includes(k)"), ("      if(field==='business_entity'&&companies.length){", "      const itemOptions=field==='item_code'&&row&&src&&editor.item_candidates?.[src];\n      if(Array.isArray(itemOptions)&&itemOptions.length&&!customerChanged&&!own(changes,row.key+'.customer_item')&&!own(changes,row.key+'.specification')){\n        controlNode=h('div',{style:{display:'grid',gap:4}},\n          input(),h('select',{'aria-label':name+'候选',value:'',disabled:inactive,\n            style:{...control,width:'100%',minWidth:0},onChange:e=>{if(e.target.value)bind(local,e.target.value,base);}},\n            h('option',{value:''},'选择匹配料号'),...itemOptions.map(code=>h('option',{key:code,value:code},code))));\n      }else if(field==='business_entity'&&companies.length){")]

def order_confirm_client(data):
    data = _customer_review_client(data)
    if hashlib.sha256(data).hexdigest() != "d377a6ed5976995ccd08bf2298ffc7bd67f4a1a60522bbeed8563decc864d14b":
        return data
    text = data.decode("utf-8")
    for old, new in ORDER_LINE_LABELS:
        text = text.replace(old, new, 1)
    data = text.encode("utf-8")
    if hashlib.sha256(data).hexdigest() != "b45db219a7f60e69a9b70164ca6cf08fa960b50ce4bc51a26c7b28da74812574":
        raise ValueError("Order line review asset mismatch")
    return data
