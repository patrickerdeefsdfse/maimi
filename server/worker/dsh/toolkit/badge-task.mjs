// One native operation for a published DSH skill. Identity and attachment ownership
// are obtained from the active task; neither is supplied by the skill or a workbook.
export const BADGE_KEY = 'employee-badges';
const fileId = {type:'string',pattern:'^pjf_[A-Za-z0-9_-]{32}$'};
export const badgeSchema = {type:'object',additionalProperties:false,required:['action'],properties:{
  action:{type:'string',enum:['template','generate','attachments']},
  attachment_id:fileId,
  allow_partial:{type:'boolean'},
  photo_overrides:{type:'array',maxItems:20,items:{type:'object',additionalProperties:false,
    required:['employee_no','attachment_id'],properties:{employee_no:{type:'string',pattern:'^M[0-9]{8}$'},attachment_id:fileId}}},
}};

export async function badgeTask(body, context, api) {
  if (context.modelTaskAuthorized !== true || !api.projects)
    throw Error('工卡制作需要登录后在云端工作区使用。');
  if (!['template','generate','attachments'].includes(body.action))
    throw Error('请选择制作工卡、下载模板或查看已上传文件。');
  const session = body.session_id;
  const original = api.snapshot(session, context);
  const alreadySelected = original.ids.includes(BADGE_KEY);
  if (!alreadySelected && original.ids.length >= 4)
    return {status:'needs_input',message:'本对话已选择四个技能，请先取消一个再选择工卡制作。'};
  // Refresh also rechecks a previously selected skill after an administrator revokes it.
  await api.select({session_id:session,plugins:alreadySelected ? original.ids : [...original.ids,BADGE_KEY]},
    {...context,expectedRevision:original.revision});
  const chosen = api.snapshot(session, context);
  async function read(operation) {
    const data = await api.projects.read(operation, session, context);
    if (api.snapshot(session,context).revision !== chosen.revision)
      throw Error('技能选择已更改，请重新选择工卡制作。');
    return data;
  }
  const args = {};
  if (body.action === 'attachments' || (body.action === 'generate' && !body.attachment_id)) {
    let current=[];
    if (body.action === 'generate') {
      const step=await read('current-step');
      current=[...new Map((step.messages||[]).flatMap(m=>m.attachments||[])
        .filter(row=>/\.xlsx$/iu.test(row.name)).map(row=>[row.attachment_id,row])).values()];
    }
    if (current.length===1) args.attachment_id=current[0].attachment_id;
    else {
      const files=current.length ? current : (await read('attachments')).attachments;
      const candidates=files.filter(row=>body.action==='attachments' ? /\.(?:xlsx|png|jpe?g|webp|bmp)$/iu.test(row.name) : /\.xlsx$/iu.test(row.name))
        .map(({name,attachment_id})=>({name,attachment_id}));
      return {status:'needs_input',message:candidates.length?'请选择本次使用的附件。':'请上传工卡名单 Excel。',attachments:candidates};
    }
  }
  if (body.action==='generate') {
    if (body.attachment_id) args.attachment_id=body.attachment_id;
    if (body.photo_overrides!==undefined) args.photo_overrides=body.photo_overrides;
    if (body.allow_partial!==undefined) args.allow_partial=body.allow_partial;
  }
  context.signal?.throwIfAborted();
  return api.invoke({operation:'project-invoke',session_id:session,connector:BADGE_KEY,action:body.action,arguments:args}, context);
}
