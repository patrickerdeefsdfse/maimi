// A migration capability gate. It never grants employee access and never
// replaces server-side IAM or file authorization.
export const REQUIREMENTS = Object.freeze([
  'nativeRuntime', 'iamAccountIsolation', 'managedModelPolicy', 'nativeHistoryReadContinue',
  'officeAuthoringRender', 'updaterTrustAndRollback', 'localSkillAuthorization',
  'localSkillInputUpload', 'localSkillOutputDownload', 'localSkillCancellation',
  'localSkillsNoPersonalCloudHost',
]);
const allowed = new Set(['NOT_TESTED', 'PASS_SYNTHETIC', 'PASS_NATIVE_LOCAL', 'PASS_COMPANY']);

export function migrationStatus(evidence) {
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) throw Error('EVIDENCE_REQUIRED');
  for (const [key, value] of Object.entries(evidence)) {
    if (!REQUIREMENTS.includes(key) || !allowed.has(value)) throw Error('INVALID_MIGRATION_EVIDENCE');
  }
  const missing = REQUIREMENTS.filter(key => evidence[key] !== 'PASS_COMPANY');
  const localSkillRequirements = REQUIREMENTS.filter(key => key.startsWith('localSkill'));
  const keepCloudEntry = localSkillRequirements.some(key => evidence[key] !== 'PASS_COMPANY');
  return { status: missing.length ? 'CANDIDATE_ONLY' : 'QUALIFIED', keepCloudEntry,
    publishCompanyInstaller: missing.length === 0, missingCompanyAcceptance: missing,
    runtimeVersion: '0.2.1-rc.2', evidenceKind: 'CALLER_SUPPLIED_NOT_AUTHORIZATION' };
}
