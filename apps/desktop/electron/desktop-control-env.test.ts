import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { removePersistedDesktopSessionToken } from './desktop-control-env'

function withTempHome(run: (home: string) => void) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-control-env-test-'))

  try {
    run(home)
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
}

test('removes only persisted desktop session-token entries', () => {
  withTempHome(home => {
    const envPath = path.join(home, '.env')
    fs.writeFileSync(
      envPath,
      [
        'OPENAI_API_KEY=keep-me',
        'HERMES_DASHBOARD_SESSION_TOKEN=stale-one',
        'export HERMES_DASHBOARD_SESSION_TOKEN = stale-two',
        'MY_HERMES_DASHBOARD_SESSION_TOKEN=also-keep',
        ''
      ].join('\r\n')
    )

    const result = removePersistedDesktopSessionToken(home)

    assert.equal(result.removed, true)
    assert.equal(
      fs.readFileSync(envPath, 'utf8'),
      ['OPENAI_API_KEY=keep-me', 'MY_HERMES_DASHBOARD_SESSION_TOKEN=also-keep', ''].join('\r\n')
    )
  })
})

test('uses the selected profile env and tolerates missing files', () => {
  withTempHome(home => {
    const profileDir = path.join(home, 'profiles', 'work')
    fs.mkdirSync(profileDir, { recursive: true })
    fs.writeFileSync(path.join(profileDir, '.env'), 'HERMES_DASHBOARD_SESSION_TOKEN=stale\nKEEP=yes\n')

    const result = removePersistedDesktopSessionToken(home, 'work')

    assert.equal(result.envPath, path.join(profileDir, '.env'))
    assert.equal(fs.readFileSync(result.envPath, 'utf8'), 'KEEP=yes\n')
    assert.equal(removePersistedDesktopSessionToken(home, 'missing').removed, false)
  })
})

test('does not rewrite an unfamiliar null-padded encoding', () => {
  withTempHome(home => {
    const envPath = path.join(home, '.env')
    const raw = Buffer.from('HERMES_DASHBOARD_SESSION_TOKEN=stale\n', 'utf16le')
    fs.writeFileSync(envPath, raw)

    const result = removePersistedDesktopSessionToken(home)

    assert.equal(result.skippedEncoding, true)
    assert.deepEqual(fs.readFileSync(envPath), raw)
  })
})
