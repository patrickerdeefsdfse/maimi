'use strict';
// A read-only installation diagnostic exits before IAM, stored credentials,
// updater, normal single-instance lock, or any business service is loaded.
if (process.argv.slice(1).includes('--self-check')) {
  void require('./self-check.cjs').runSelfCheck();
} else {
  require('./main.cjs');
}
