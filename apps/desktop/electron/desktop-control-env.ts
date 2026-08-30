import fs from 'node:fs'
import path from 'node:path'

const DESKTOP_SESSION_TOKEN_KEY = 'HERMES_DASHBOARD_SESSION_TOKEN'

function profileHome(hermesHome: string, profile: string): string {
  return profile && profile !== 'default' ? path.join(hermesHome, 'profiles', profile) : hermesHome
}

/** Remove a legacy persisted copy of Desktop's per-process credential. */
export function removePersistedDesktopSessionToken(
  hermesHome: string,
  profile = 'default'
): { envPath: string; removed: boolean; skippedEncoding: boolean } {
  const envPath = path.join(profileHome(hermesHome, profile), '.env')

  let raw: Buffer

  try {
    raw = fs.readFileSync(envPath)
  } catch (error: any) {
    if (error?.code === 'ENOENT') {
      return { envPath, removed: false, skippedEncoding: false }
    }

    throw error
  }

  // Avoid corrupting UTF-16 or another unfamiliar encoding. Hermes' normal
  // env-file sanitizer can canonicalize it before a later Desktop start.
  if (raw.includes(0)) {
    return { envPath, removed: false, skippedEncoding: true }
  }

  const text = raw.toString('utf8')

  const line = new RegExp(
    `^[ \\t]*(?:export[ \\t]+)?${DESKTOP_SESSION_TOKEN_KEY}[ \\t]*=.*(?:\\r?\\n|$)`,
    'gim'
  )

  const sanitized = text.replace(line, '')

  if (sanitized === text) {
    return { envPath, removed: false, skippedEncoding: false }
  }

  const tempPath = `${envPath}.desktop-session-migration-${process.pid}.tmp`

  try {
    fs.writeFileSync(tempPath, sanitized, { encoding: 'utf8', mode: fs.statSync(envPath).mode })
    fs.renameSync(tempPath, envPath)
  } catch (error) {
    try {
      fs.unlinkSync(tempPath)
    } catch {
      void 0
    }

    throw error
  }

  return { envPath, removed: true, skippedEncoding: false }
}
