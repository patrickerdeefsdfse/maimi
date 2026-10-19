/** Company-only action using the native task lock, quit confirmation, and backend close. */
export interface CompanySignOutTarget {
 readonly identity: object
 lock(): Promise<unknown>
 unlock(): Promise<unknown>
 blockResume(): Promise<void>
 stop(): Promise<void>
 revoke(): Promise<boolean>
}
export interface CompanySignOutOptions {
 current(): CompanySignOutTarget | undefined
 confirmAccount(): Promise<boolean>
 confirmTasks(): Promise<boolean>
 notify(result: 'revocation-pending' | 'stop-failed' | 'not-completed'): Promise<void>
 finish(): void
}

export class CompanySignOutController {
 private pending: Promise<boolean> | undefined
 private committed = false
 constructor(private readonly options: CompanySignOutOptions) {}
 run(): Promise<boolean> {
  if (this.pending !== undefined) return this.pending
  if (this.committed) return Promise.resolve(false)
  this.pending = this.decide().finally(() => { this.pending = undefined })
  return this.pending
 }
 private async report(result: 'revocation-pending' | 'stop-failed' | 'not-completed'): Promise<void> {
  try { await this.options.notify(result) } catch { /* notifications never undo sign-out */ }
 }
 private async decide(): Promise<boolean> {
  const target = this.options.current()
  if (target === undefined) return false
  let locked = false
  const same = (): boolean => this.options.current()?.identity === target.identity
  try {
   if (!await this.options.confirmAccount() || !same()) return false
   // Lock first, so no newly submitted work can appear after task confirmation.
   locked = true; await target.lock()
   if (!same() || !await this.options.confirmTasks() || !same()) return false
   await target.blockResume()
   this.committed = true
   try { await target.stop() }
   catch { await this.report('stop-failed'); return false }
   let revoked = false
   try { revoked = await target.revoke() } catch { /* persisted local block remains */ }
   if (!revoked) await this.report('revocation-pending')
   return true
  } catch {
   await this.report('not-completed')
   return false
  } finally {
   if (this.committed) this.options.finish()
   else if (locked) {
    try { await target.unlock() } catch { await this.report('not-completed') }
   }
  }
 }
}
