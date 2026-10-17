import {LocalCredentialProvider} from '/opt/dsh/node_modules/@deepseek-ai/dsh-credentials-local/lib/index.js';
import {scopedCredentialProvider} from './native-model-scope.mjs';
import {resolveModelCredential} from './native-task-context.mjs';
export default scopedCredentialProvider(LocalCredentialProvider,{resolveCredential:resolveModelCredential});
