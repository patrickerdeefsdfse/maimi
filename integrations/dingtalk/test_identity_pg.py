import concurrent.futures
import dataclasses
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,"/proof")
import psycopg
from routing import Channel, ReceivedText, Refused
from identity_store import IdentityStore

# Only an ephemeral isolated test container is used; no external DSN override.
def transaction():
    return psycopg.connect(host='127.0.0.1',dbname='postgres',user='postgres',connect_timeout=3)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.channel=Channel("weixin","robot-a","install-a")
        self.store=IdentityStore(b'k'*32)
        self.now=2_000_000_000
        self.employee="M92000001"
        self.event=ReceivedText(self.channel,"peer-one","private-dialog","event-one","hello",self.now,True)
        with transaction() as db:
            db.execute("TRUNCATE maimi_im.binding,maimi_im.bind_attempt,maimi_im.receipt")

    def two_ends(self, subject="peer-one", employee=None):
        with transaction() as db:
            ticket,claim=self.store.begin(db,self.channel,employee or self.employee,self.now)
        with transaction() as db:
            proof=self.store.claim(db,self.channel,subject,claim,self.now)
        return ticket,proof

    def bind(self):
        ticket,proof=self.two_ends()
        with transaction() as db:
            return self.store.confirm(db,self.channel,self.employee,ticket,proof,self.now)

    def test_pairing_is_private_one_use_and_tenant_scoped(self):
        with transaction() as db:
            ticket,code=self.store.begin(db,self.channel,self.employee,self.now)
        with self.assertRaises(Refused), transaction() as db:
            self.store.claim(db,Channel("weixin","robot-b","install-a"),"other",code,self.now)
        with transaction() as db:
            proof=self.store.claim(db,self.channel,"peer-one",code,self.now)
        with self.assertRaises(Refused),transaction() as db:
            self.store.claim(db,self.channel,"other",code,self.now)
        with self.assertRaises(Refused),transaction() as db:
            self.store.confirm(db,self.channel,"M92000002",ticket,proof,self.now)
        with transaction() as db:
            self.store.confirm(db,self.channel,self.employee,ticket,proof,self.now)
            row=db.execute("SELECT account,peer_hash FROM maimi_im.binding").fetchone()
            self.assertTrue(all(len(v)==64 for v in row))
        with self.assertRaises(Refused),transaction() as db:
            self.store.confirm(db,self.channel,self.employee,ticket,proof,self.now)

    def test_existing_binding_cannot_be_reassigned(self):
        self.bind()
        for subject,employee in [('peer-one','M92000002'),('peer-two',self.employee)]:
            ticket,proof=self.two_ends(subject,employee)
            with self.assertRaises(Refused),transaction() as db:
                self.store.confirm(db,self.channel,employee,ticket,proof,self.now)
        with transaction() as db:
            self.assertEqual(self.store.get_locked(db,self.channel,"peer-one",self.now).employee,self.employee)

    def test_age_revoke_cleanup(self):
        ticket,proof=self.two_ends()
        with self.assertRaises(Refused),transaction() as db:
            self.store.confirm(db,self.channel,self.employee,ticket,proof,self.now+300)
        self.bind()
        with transaction() as db:
            route=self.store.reserve_inbound(db,self.event,lambda _:True,self.now,b's'*32)
            self.store.finish(db,route,"uncertain")
        with transaction() as db:
            self.store.revoke(db,self.channel,self.employee)
        with self.assertRaises(Refused),transaction() as db:
            self.store.reserve_inbound(db,self.event,lambda _:True,self.now,b's'*32)
        with transaction() as db:
            self.store.cleanup(db,self.now+86401)
            self.assertFalse(db.execute("SELECT 1 FROM maimi_im.receipt").fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM maimi_im.bind_attempt").fetchone())

    def test_concurrent_redelivery_across_receivers_dispatches_once(self):
        self.bind()
        def reserve(_):
            try:
                with transaction() as db:
                    self.store.reserve_inbound(db,self.event,lambda _:True,self.now,b's'*32)
                return 'reserved'
            except Refused as e:
                return str(e)
        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            results=list(executor.map(reserve,range(2)))
        self.assertCountEqual(results,['reserved','DUPLICATE_OR_PREVIOUS_OUTCOME_UNKNOWN'])

if __name__ == '__main__':
    with transaction() as db:
        db.execute(Path('/proof/identity-schema.sql').read_text())
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DatabaseTests))
    Path('/proof/postgres-validation.json').write_text(json.dumps(dict(database='temporary isolated PostgreSQL 16', tests=result.testsRun, success=result.wasSuccessful(), messages_sent=0, production_changes=0),indent=2))
    raise SystemExit(0 if result.wasSuccessful() else 1)

