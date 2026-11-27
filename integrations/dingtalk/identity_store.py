"""PostgreSQL transaction primitives for the company relay pilot.

Every public function takes a transaction (autocommit disabled) and an
authenticated caller. No platform login or HTTP entry is exposed here.
Secrets returned by begin/claim are transient and must never be logged.
"""
from hashlib import sha256
import hmac
import json
import secrets
import uuid

from routing import Binding, Channel, OWNER, Refused, _id


class IdentityStore:
    def __init__(self, key: bytes):
        if not isinstance(key, bytes) or len(key)<32:
            raise Refused("BINDING_KEY_INVALID")
        self.key=key

    def _key(self, *parts):
        raw=json.dumps(parts,ensure_ascii=False,separators=(",",":")).encode("utf8")
        return hmac.new(self.key,raw,sha256).hexdigest()

    def _account(self, channel):
        if channel.provider not in ("weixin","dingtalk"):
            raise Refused("CHANNEL_UNKNOWN")
        return self._key("account",channel.provider,_id(channel.tenant),_id(channel.bot))

    @staticmethod
    def _lock_employee(db, account, employee):
        if not isinstance(employee,str) or not OWNER.fullmatch(employee):
            raise Refused("IAM_EMPLOYEE_REQUIRED")
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",(account+employee,))

    def begin(self, db, channel: Channel, employee: str, now: int):
        """Called only after IAM login. Show claim in that employee's browser."""
        if not isinstance(employee,str) or not OWNER.fullmatch(employee):
            raise Refused("IAM_EMPLOYEE_REQUIRED")
        account=self._account(channel)
        self._lock_employee(db,account,employee)
        ticket=str(uuid.uuid4())
        claim=secrets.token_urlsafe(20)
        db.execute("""INSERT INTO maimi_im.bind_attempt(ticket,employee,account,claim_hash,expires_at)
                      VALUES(%s,%s,%s,%s,%s)""",(ticket,employee,account,self._key("claim",account,claim),now+300))
        return ticket,claim

    def claim(self, db, channel: Channel, subject: str, claim: str, now: int):
        """Called only for a verified private peer. Reply proof solely in that chat."""
        account=self._account(channel)
        peer=self._key("peer",account,_id(subject))
        proof=secrets.token_urlsafe(20)
        result=db.execute("""UPDATE maimi_im.bind_attempt SET peer_hash=%s,proof_hash=%s
                             WHERE account=%s AND claim_hash=%s AND peer_hash IS NULL AND expires_at>%s
                             RETURNING ticket""",(peer,self._key("proof",account,proof),account,
                                                  self._key("claim",account,_id(claim)),now))
        if result.fetchone() is None:
            raise Refused("CHALLENGE_INVALID")
        return proof

    def confirm(self, db, channel, employee, ticket, proof, now):
        """In the same employee's IAM browser; proof must have come from the private chat."""
        account=self._account(channel)
        self._lock_employee(db,account,employee)
        attempt=db.execute("""DELETE FROM maimi_im.bind_attempt WHERE ticket=%s AND employee=%s
                              AND account=%s AND proof_hash=%s AND expires_at>%s RETURNING peer_hash""",
                           (ticket,employee,account,self._key("proof",account,_id(proof)),now)).fetchone()
        if not attempt:
            raise Refused("PROOF_INVALID")
        peer=attempt[0]
        generation=uuid.uuid4()
        result=db.execute("""INSERT INTO maimi_im.binding VALUES(%s,%s,%s,%s,%s,%s)
                             ON CONFLICT DO NOTHING RETURNING generation""",
                          (account,peer,employee,generation,now,now+30*86400))
        if not result.fetchone():
            raise Refused("EXISTING_BINDING")
        return str(generation)

    def revoke(self, db, channel, employee):
        # Caller must verify the IAM employee; serialized with message claims.
        self._lock_employee(db,self._account(channel),employee)
        db.execute("DELETE FROM maimi_im.binding WHERE account=%s AND employee=%s",
                   (self._account(channel),employee))
        db.execute("DELETE FROM maimi_im.bind_attempt WHERE account=%s AND employee=%s",
                   (self._account(channel),employee))

    def get_locked(self, db, channel, subject, now):
        account=self._account(channel)
        row=db.execute("""SELECT employee,generation,valid_since,valid_until FROM maimi_im.binding
                          WHERE account=%s AND peer_hash=%s AND valid_until>%s FOR UPDATE""",
                       (account,self._key("peer",account,_id(subject)),now)).fetchone()
        if not row:
            raise Refused("EMPLOYEE_BINDING_REQUIRED")
        return Binding(channel,subject,row[0],str(row[1]),row[2],row[3],True)

    def reserve_inbound(self, db, event, active_employee, now, routing_key):
        from routing import resolve
        binding=self.get_locked(db,event.channel,event.sender,now)
        route=resolve(event,[binding],active_employee,now=now,routing_key=routing_key)
        inserted=db.execute("""INSERT INTO maimi_im.receipt VALUES(%s,%s,%s,'reserved')
                               ON CONFLICT DO NOTHING RETURNING receipt_key""",
                            (route.receipt_key,route.binding_generation,now)).fetchone()
        if not inserted:
            raise Refused("DUPLICATE_OR_PREVIOUS_OUTCOME_UNKNOWN")
        return route

    @staticmethod
    def finish(db, route, state):
        if state not in ("finished","uncertain"):
            raise Refused("OUTCOME_INVALID")
        db.execute("""UPDATE maimi_im.receipt SET state=%s WHERE receipt_key=%s
                      AND generation=%s AND state='reserved'""",(state, route.receipt_key,route.binding_generation))

    @staticmethod
    def cleanup(db, now):
        db.execute("DELETE FROM maimi_im.bind_attempt WHERE expires_at<=%s",(now,))
        db.execute("DELETE FROM maimi_im.binding WHERE valid_until<=%s",(now,))
        # Acceptance is restricted to fresh events. Keep receipts beyond the full replay window.
        db.execute("DELETE FROM maimi_im.receipt WHERE reserved_at<%s",(now-86400,))

