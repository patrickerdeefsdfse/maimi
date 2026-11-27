import {AsyncLocalStorage} from 'node:async_hooks';

// This context is only entered by the authenticated worker's private prompt dispatch.
const documents = new AsyncLocalStorage();
export function runOrderDocumentPrompt(request, enabled, invoke) {
  if (!enabled) return invoke();
  const receipts = new Set(request.content.filter(part => part.type === 'file').map(part => part.receiptId));
  return documents.run({sessionId:request.sessionId, receipts}, invoke);
}
export function orderDocumentPart(agent, id, staged) {
  const current = documents.getStore();
  if (staged?.type !== 'image' || !current || current.sessionId !== agent.session.id || !current.receipts.has(id)) return staged;
  const {attachmentId, name, bytes} = staged.attachment;
  return {type:'file', attachment:{attachmentId, name:name || 'image', bytes}};
}
