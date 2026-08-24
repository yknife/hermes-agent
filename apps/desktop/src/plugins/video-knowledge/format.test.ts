import { describe, expect, it } from 'vitest'

import { beijingDateTime } from './format'

describe('Beijing task timestamps', () => {
  it('treats timezone-less database timestamps as UTC', () => {
    expect(beijingDateTime('2026-08-22T09:49:05')).toBe('2026-08-22 17:49:05')
  })

  it('normalizes timezone-aware timestamps to Asia/Shanghai', () => {
    expect(beijingDateTime('2026-08-22T01:49:05-08:00')).toBe('2026-08-22 17:49:05')
    expect(beijingDateTime('2026-08-22T09:49:05Z')).toBe('2026-08-22 17:49:05')
  })

  it('handles malformed timestamps without showing an invalid date', () => {
    expect(beijingDateTime('not-a-date')).toBe('—')
  })
})
