from pathlib import Path
import hashlib,json
root=Path(__file__).resolve().parent
names=['history-native.mjs','history-migration.mjs','history-runtime-snapshot.mjs','history-admission-rc2.mjs','history-server.mjs']
result={
 'schema':'history-runtime-020-independent-review-v1','status':'PASS','company_requests':0,
 'review_scope':['owner volume isolation','original log byte preservation','predecessor receipt preservation','explicit deletion snapshot cleanup','legacy incomplete transaction refusal','admission before native process startup'],
 'source_sha256':{name:hashlib.sha256((root/'overlay/opt/personal-cloud'/name).read_bytes()).hexdigest() for name in names},
 'findings':[{'id':'R41-HISTORY-01','severity':'P1','status':'FIXED_AND_TESTED','problem':'A complete rc17 incremental receipt followed by newly imported V3 history was replaced by a new rc20 batch without an independent copy of the previous receipt.','resolution':'The first rc20 snapshot now saves prior-r29-receipt.json as independent original bytes with pinned hash and recoverable stage/link/fsync publication before any current journal updates.','tests':['mixed completed rc17 receipt with newly imported V3 retains exact prior receipt independently','prior receipt staging and link interruptions recover with exact predecessor metadata']}],
 'validation':{
  'official_codec_and_continuation':{'tests':34,'passed':34,'log':'history020-prebuild.log','reviewer_action':'Read complete successful local synthetic test log; independently reviewed implementation and added separate ownership/fence tests.'},
  'independent_ownership_and_fence':{'tests':4,'passed':4,'log':'history-independent-review.log','runner':'test-history-independent-review.sh','environment':'Existing local Node container, network none, uid 1000, read-only root, synthetic tmpfs state.'},
  'owner_isolation':'Manager requires owner-keyed volume name and persisted instance/owner labels, no existing volume references, then mounts only that state volume as /state. Desktop uses hashed account home and outer account lease. No caller-supplied filesystem path enters the cloud helper.',
  'byte_preservation':'Snapshot payload is written to a new stage file, fsynced, published with no-replace link and stage unlink. Source inode is not reused; current V4 may append while saved bytes remain immutable and hash-checked.',
  'receipts':'Complete rc17 receipt stays byte-identical on ordinary admission. A later batch may update the active incremental journal, while the exact pre-rc20 journal bytes remain in the independent snapshot. The older alpha migration receipt remains untouched.',
  'deletion':'Explicit delete records runtime-snapshot source identity in the durable existing delete journal, removes owned raw snapshot payload along with current session generations, preserves shared attachments/files and metadata-only receipts.',
  'incomplete_old_migrations':'Old alpha pending receipt refuses MIGRATION_RECEIPT_PENDING; rc17 incremental publishing refuses MIGRATION_PRIOR_RUNTIME_PENDING before snapshot or current codec publication.',
  'startup_order':'Cloud open requires activation, owner-scoped helper admission returns READY before worker creation. Desktop guardDataFormat admission executes under owned account lease before native spawn. Official rc20 persistence constructor/locate used during inventory do not open write handles or mutate session bytes.'
 },
 'limits':['Synthetic histories only; no company conversations, credentials, VMware or model calls.','Full npm/runtime image build and desktop installer verification are handled by the owning agents separately.','Independent snapshot format is intentionally one original pre-rc20 generation, not a continuous backup of new rc20 conversations.']
}
(root/'history-independent-review.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':'WRITTEN','independent_tests':4,'official_codec_tests':34,'source_sha256':result['source_sha256']}))
