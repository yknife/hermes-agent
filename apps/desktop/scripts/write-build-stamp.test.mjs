import assert from 'node:assert/strict'
import { test } from 'vitest'

import {
  DEFAULT_REPOSITORY,
  FALLBACK_BRANCH,
  FALLBACK_COMMIT,
  fromCI,
  fromFallback,
  fromLocalGit,
  isFallbackCommit,
  resolveStamp
} from './write-build-stamp.mjs'

test('fromCI reads GITHUB_SHA / GITHUB_REF_NAME', () => {
  assert.deepEqual(
    fromCI({ GITHUB_SHA: 'a'.repeat(40), GITHUB_REF_NAME: 'release' }),
    {
      commit: 'a'.repeat(40),
      branch: 'release',
      repository: DEFAULT_REPOSITORY,
      dirty: false,
      source: 'ci'
    }
  )
  assert.equal(fromCI({}), null)
})

test('fromLocalGit returns null when git rev-parse fails', () => {
  const stamp = fromLocalGit('/tmp/not-a-repo', () => null)
  assert.equal(stamp, null)
})

test('fromLocalGit reads HEAD + branch + dirty status', () => {
  const calls = []
  const execFn = (cmd) => {
    calls.push(cmd)
    if (cmd === 'git rev-parse HEAD') return 'b'.repeat(40)
    if (cmd === 'git rev-parse --abbrev-ref HEAD') return 'main'
    if (cmd === 'git status --porcelain -uno') return ' M apps/desktop/package.json'
    if (cmd === 'git remote get-url origin') return 'https://github.com/yknife/hermes-agent.git'
    return null
  }
  assert.deepEqual(fromLocalGit('/repo', execFn), {
    commit: 'b'.repeat(40),
    branch: 'main',
    repository: 'yknife/hermes-agent',
    dirty: true,
    source: 'local'
  })
  assert.ok(calls.includes('git rev-parse HEAD'))
})

test('fromFallback uses the all-zero placeholder commit', () => {
  assert.deepEqual(fromFallback(), {
    commit: FALLBACK_COMMIT,
    branch: FALLBACK_BRANCH,
    repository: DEFAULT_REPOSITORY,
    dirty: false,
    source: 'fallback'
  })
  assert.equal(isFallbackCommit(FALLBACK_COMMIT), true)
  assert.equal(isFallbackCommit('a'.repeat(40)), false)
})

test('resolveStamp prefers CI over local git over fallback', () => {
  const ci = resolveStamp({
    env: { GITHUB_SHA: 'c'.repeat(40), GITHUB_REF_NAME: 'main' },
    execFn: () => 'should-not-run'
  })
  assert.equal(ci.source, 'ci')
  assert.equal(ci.commit, 'c'.repeat(40))

  const local = resolveStamp({
    env: {},
    execFn: (cmd) => {
      if (cmd === 'git rev-parse HEAD') return 'd'.repeat(40)
      if (cmd === 'git rev-parse --abbrev-ref HEAD') return 'main'
      if (cmd === 'git status --porcelain -uno') return ''
      return null
    }
  })
  assert.equal(local.source, 'local')
  assert.equal(local.commit, 'd'.repeat(40))
  assert.equal(local.dirty, false)
})

test('explicit release metadata wins over outer GitHub workflow metadata', () => {
  const stamp = fromCI({
    GITHUB_SHA: 'a'.repeat(40),
    GITHUB_REF_NAME: 'main',
    GITHUB_REPOSITORY: 'yknife/video-knowledge-collector',
    HERMES_DESKTOP_BOOTSTRAP_COMMIT: 'b'.repeat(40),
    HERMES_DESKTOP_BOOTSTRAP_BRANCH: 'vkc-integration',
    HERMES_DESKTOP_BOOTSTRAP_REPOSITORY: 'yknife/hermes-agent'
  })

  assert.deepEqual(stamp, {
    commit: 'b'.repeat(40),
    branch: 'vkc-integration',
    repository: 'yknife/hermes-agent',
    dirty: false,
    source: 'release'
  })
})

test('resolveStamp falls back when neither CI nor git is available', () => {
  const stamp = resolveStamp({ env: {}, execFn: () => null })
  assert.deepEqual(stamp, {
    commit: FALLBACK_COMMIT,
    branch: FALLBACK_BRANCH,
    repository: DEFAULT_REPOSITORY,
    dirty: false,
    source: 'fallback'
  })
})
