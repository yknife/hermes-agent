import assert from 'node:assert/strict'

import { test } from 'vitest'

import { INSTALL_STAMP_SCHEMA_VERSION, parseInstallStamp } from './install-stamp'

test('schema v2 install stamps retain the bootstrap repository', () => {
  const stamp = parseInstallStamp(
    {
      schemaVersion: 2,
      commit: 'a'.repeat(40),
      branch: 'vkc-integration',
      repository: 'yknife/hermes-agent',
      builtAt: '2026-08-24T00:00:00.000Z',
      dirty: false,
      source: 'release'
    },
    'C:\\Program Files\\Hermes\\resources\\install-stamp.json'
  )

  assert.equal(INSTALL_STAMP_SCHEMA_VERSION, 2)
  assert.equal(stamp?.repository, 'yknife/hermes-agent')
  assert.equal(stamp?.commit, 'a'.repeat(40))
})

test('obsolete or malformed install stamps are rejected', () => {
  assert.equal(parseInstallStamp({ schemaVersion: 1, commit: 'a'.repeat(40) }, 'old.json'), null)
  assert.equal(parseInstallStamp({ schemaVersion: 2, commit: 'short' }, 'bad.json'), null)
})
