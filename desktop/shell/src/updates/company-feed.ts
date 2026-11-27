/** The application, blockmaps and channel manifests share one company-owned origin. */
export const companyUpdateFeed = 'https://desktop-gateway.example.invalid/maimi/updates/windows/'

export function isCompanyUpdateRequest(raw: string): boolean {
  let url: URL
  try { url = new URL(raw) } catch { return false }
  if (url.username || url.password || url.hash || !url.href.startsWith(companyUpdateFeed)) return false
  const name = url.pathname.slice('/maimi/updates/windows/'.length)
  return /^(?:(?:latest|stable|nightly)\.yml|Maimi-Setup-[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?-x64\.exe(?:\.blockmap)?)$/.test(name)
}

export function isCompanyInstaller(version: string, files: readonly {url: string; sha512: string}[]): boolean {
  if (!Array.isArray(files) || files.length !== 1) return false
  const candidate = files[0]
  if (!candidate || typeof candidate.url !== 'string' || typeof candidate.sha512 !== 'string') return false
  const name = `Maimi-Setup-${version}-x64.exe`
  return (candidate.url === name || candidate.url === companyUpdateFeed + name) &&
    /^[A-Za-z0-9+/]{86}==$/.test(candidate.sha512)
}
