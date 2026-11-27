import { createHash } from 'node:crypto';
import { link, mkdir, open } from 'node:fs/promises';
import { basename, dirname, join } from 'node:path';

// The caller owns a fresh, private directory. The marker is published only after this returns.
export async function writeSharedCopy(root, alias, ref, chunks, signal) {
  const digest = ref.attachmentId.slice(7);
  const prefix = digest.slice(0, 2);
  if (!/^[a-f0-9]{64}$/.test(digest) || !Number.isSafeInteger(ref.bytes) || ref.bytes < 0 ||
      dirname(alias) !== join(root, 'files', prefix, digest) || basename(alias) !== ref.name) {
    throw new Error('Invalid attachment cache reference');
  }
  const objectDir = join(root, 'file-objects', prefix);
  await mkdir(objectDir, {recursive: true, mode: 0o700});
  const object = join(objectDir, digest);
  const handle = await open(object, 'wx', 0o600);
  const hash = createHash('sha256');
  let size = 0;
  try {
    for await (const chunk of chunks) {
      signal?.throwIfAborted();
      size += chunk.byteLength;
      if (size > ref.bytes) throw new Error('Attachment size changed');
      hash.update(chunk);
      await handle.writeFile(chunk);
    }
    signal?.throwIfAborted();
    if (size !== ref.bytes || hash.digest('hex') !== digest) throw new Error('Attachment digest changed');
    await handle.chmod(0o400);
    await handle.sync();
  } finally {
    await handle.close();
  }
  await mkdir(dirname(alias), {recursive: true, mode: 0o700});
  await link(object, alias);
}
