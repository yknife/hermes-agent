export function durationLabel(seconds: null | number): string {
  if (seconds == null) {
    return '时长未知'
  }

  const total = Math.floor(seconds)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const rest = total % 60

  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`
    : `${minutes}:${String(rest).padStart(2, '0')}`
}

export function timestamp(milliseconds: number): string {
  return durationLabel(milliseconds / 1000)
}

const beijingDateTimeFormatter = new Intl.DateTimeFormat('zh-CN-u-nu-latn', {
  day: '2-digit',
  hour: '2-digit',
  hourCycle: 'h23',
  minute: '2-digit',
  month: '2-digit',
  second: '2-digit',
  timeZone: 'Asia/Shanghai',
  year: 'numeric'
})

export function beijingDateTime(value: string): string {
  const trimmed = value.trim()
  const normalized = trimmed.includes('T') ? trimmed : trimmed.replace(' ', 'T')
  const timezoneAware = /(?:z|[+-]\d{2}:?\d{2})$/i.test(normalized)
  const date = new Date(timezoneAware ? normalized : `${normalized}Z`)

  if (Number.isNaN(date.valueOf())) {
    return '—'
  }

  const parts = Object.fromEntries(
    beijingDateTimeFormatter.formatToParts(date).map(part => [part.type, part.value])
  )

  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`
}

export function fileSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`
  }

  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`
  }

  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function apiErrorPayload(value: unknown): {
  code: null | string
  message: string
} {
  const raw = value instanceof Error && value.message
    ? value.message
    : typeof value === 'object' && value !== null && 'message' in value && typeof value.message === 'string'
      ? value.message
      : typeof value === 'string'
        ? value
        : ''

  const unwrapped = raw.match(/Error invoking remote method '[^']+': Error: (.+)$/)?.[1] ?? raw
  const jsonStart = unwrapped.indexOf('{')

  if (jsonStart >= 0) {
    try {
      const payload = JSON.parse(unwrapped.slice(jsonStart)) as {
        detail?: { code?: unknown; message?: unknown } | string
        error?: { code?: unknown; message?: unknown }
      }

      const detail = typeof payload.detail === 'object' && payload.detail !== null ? payload.detail : payload.error

      if (detail && typeof detail.message === 'string' && detail.message.trim()) {
        return {
          code: typeof detail.code === 'string' ? detail.code : null,
          message: detail.message.trim()
        }
      }

      if (typeof payload.detail === 'string' && payload.detail.trim()) {
        return { code: null, message: payload.detail.trim() }
      }
    } catch {
      // Preserve the original safe fallback when an upstream error is not JSON.
    }
  }

  return { code: null, message: unwrapped.replace(/^\d{3}:\s*/, '').trim() }
}

export function errorCode(value: unknown): null | string {
  return apiErrorPayload(value).code
}

export function errorMessage(value: unknown, fallback = '请求失败'): string {
  return apiErrorPayload(value).message || fallback
}

export function readableContent(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2)
}
