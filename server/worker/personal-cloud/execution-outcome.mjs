// A positive receipt applies to this RPC only. It cannot certify a previously spawned job.
export const receiptSchema = 'maimi-execution-outcome/v1';
const completedReads = new Set([
 'fs.resolve','fs.lstat','fs.stat','fs.readText','fs.readBytes','fs.readByteRange','fs.listDir','document.read','process.resolve'
]);
const knownOps = new Set([...completedReads,'fs.writeText','fs.editText','process.spawn','process.poll','process.stdin','process.terminate','process.forget']);
export const operationLabel = operation => knownOps.has(operation) ? operation : 'other';
export const isRead = operation => completedReads.has(operation);
const proof = Symbol('execution receipt');
export function beforeDispatchError() {
 const error = new Error('本次工具操作尚未开始，请稍后重试。');
 error[proof] = 'not_started';
 return error;
}
export function remoteError(status, record, operation) {
 if (record?.schema === receiptSchema && record.operation === operation) {
  if (status === 409 && record.disposition === 'not_started') return beforeDispatchError();
  if (status === 409 && record.disposition === 'finished_error' && isRead(operation)) {
   const error = new Error('执行节点已返回读取失败。请检查文件路径、格式或读取权限。');
   error[proof] = 'finished_error';
   return error;
  }
 }
 return new Error('本次工具操作的结果尚未确认，系统已停止自动重试。');
}
export const confirmedFailure = error => error?.[proof];
