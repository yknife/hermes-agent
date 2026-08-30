export const INSTALL_STAMP_SCHEMA_VERSION = 2

export type InstallStamp = Readonly<{
  schemaVersion: number
  commit: string
  branch: string | null
  repository: string | null
  builtAt: string | null
  dirty: boolean
  source: string | null
  path: string
}>

/**
 * Validate and normalize the build-time metadata used by first-launch
 * bootstrap. Schema v2 added `repository`, which must survive parsing so a
 * fork build fetches its installer and checkout from the matching fork.
 */
export function parseInstallStamp(parsed: unknown, stampPath: string): InstallStamp | null {
  if (!parsed || typeof parsed !== 'object') {
    return null
  }

  const value = parsed as Record<string, unknown>

  if (value.schemaVersion !== INSTALL_STAMP_SCHEMA_VERSION) {
    return null
  }

  if (typeof value.commit !== 'string' || value.commit.length < 7) {
    return null
  }

  return Object.freeze({
    schemaVersion: value.schemaVersion,
    commit: value.commit,
    branch: typeof value.branch === 'string' && value.branch ? value.branch : null,
    repository: typeof value.repository === 'string' && value.repository ? value.repository : null,
    builtAt: typeof value.builtAt === 'string' && value.builtAt ? value.builtAt : null,
    dirty: Boolean(value.dirty),
    source: typeof value.source === 'string' && value.source ? value.source : null,
    path: stampPath
  })
}
