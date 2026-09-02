import { describe, expect, it } from 'vitest'

import { beijingDateTime, errorCode, errorMessage } from './format'

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

describe('Video Knowledge errors', () => {
  it('unwraps a plugin API media error for inline display', () => {
    const error = new Error(
      'Error invoking remote method \'hermes:api\': Error: 422: {"detail":{"code":"MEDIA_UNAVAILABLE","message":"该视频目前不可用，可能已删除、设为私密或受地区限制","details":{}}}'
    )

    expect(errorMessage(error)).toBe('该视频目前不可用，可能已删除、设为私密或受地区限制')
    expect(errorCode(error)).toBe('MEDIA_UNAVAILABLE')
  })

  it('still unwraps a non-JSON IPC error', () => {
    expect(errorMessage(new Error("Error invoking remote method 'hermes:api': Error: network failed")))
      .toBe('network failed')
  })
})
