import { createReadStream } from 'node:fs'
import { stat } from 'node:fs/promises'
import path from 'node:path'
import { Readable } from 'node:stream'

const STREAMABLE_MEDIA_EXTENSIONS = [
  '.avi',
  '.flac',
  '.jpeg',
  '.jpg',
  '.m4a',
  '.mkv',
  '.mov',
  '.mp3',
  '.mp4',
  '.ogg',
  '.opus',
  '.png',
  '.wav',
  '.webm',
  '.webp'
] as const

const FORWARDED_MEDIA_REQUEST_HEADERS = ['accept', 'if-modified-since', 'if-none-match', 'if-range', 'range'] as const

export const MEDIA_PROTOCOL = 'hermes-media'

const MEDIA_CONTENT_TYPES: Record<string, string> = {
  '.avi': 'video/x-msvideo',
  '.flac': 'audio/flac',
  '.jpeg': 'image/jpeg',
  '.jpg': 'image/jpeg',
  '.m4a': 'audio/mp4',
  '.mkv': 'video/x-matroska',
  '.mov': 'video/quicktime',
  '.mp3': 'audio/mpeg',
  '.mp4': 'video/mp4',
  '.ogg': 'audio/ogg',
  '.opus': 'audio/ogg',
  '.png': 'image/png',
  '.wav': 'audio/wav',
  '.webm': 'video/webm',
  '.webp': 'image/webp'
}

type MediaProtocolMode = 'remote' | 'stream'

interface MediaProtocolTarget {
  filePath: string
  mode: MediaProtocolMode
  profile?: string
}

export interface MediaRemoteConnection {
  authMode?: 'oauth' | 'token'
  baseUrl: string
  mode?: 'local' | 'remote'
  token?: null | string
}

type MediaRequestMethod = 'GET' | 'HEAD'

export interface MediaProtocolDependencies {
  ensureRemoteBearer: (baseUrl: string) => Promise<null | string>
  fetchLocal: (resolvedPath: string, headers: Headers, method: MediaRequestMethod) => Promise<Response>
  fetchRemote: (url: string, headers: Headers, method: MediaRequestMethod) => Promise<Response>
  fetchRemoteWithCookies: (url: string, headers: Headers, method: MediaRequestMethod) => Promise<Response>
  resolveLocalFile: (filePath: string) => Promise<string>
  resolveRemoteConnection: (profile?: string) => Promise<MediaRemoteConnection>
}

function parseMediaProtocolTarget(rawUrl: string): MediaProtocolTarget {
  const url = new URL(rawUrl)
  const mode = url.hostname as MediaProtocolMode

  if (mode !== 'remote' && mode !== 'stream') {
    throw new Error('Unsupported media protocol target')
  }

  const filePath = decodeURIComponent(url.pathname.replace(/^\/+/, ''))

  if (!filePath) {
    throw new Error('Missing media path')
  }

  const profile = url.searchParams.get('profile')?.trim() || undefined

  return { filePath, mode, profile }
}

export function isStreamableMediaPath(filePath: string): boolean {
  const lower = filePath.toLowerCase()

  return STREAMABLE_MEDIA_EXTENSIONS.some(extension => lower.endsWith(extension))
}

export function mediaRequestHeaders(source: Headers): Headers {
  const forwarded = new Headers()

  for (const name of FORWARDED_MEDIA_REQUEST_HEADERS) {
    const value = source.get(name)

    if (value) {
      forwarded.set(name, value)
    }
  }

  return forwarded
}

export function remoteMediaEndpoint(baseUrl: string, filePath: string): string {
  const normalizedBase = baseUrl.replace(/\/+$/, '')
  const url = new URL(`${normalizedBase}/api/files/stream`)

  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error(`Unsupported Hermes backend URL protocol: ${url.protocol}`)
  }

  url.searchParams.set('path', filePath)

  return url.toString()
}

interface ByteRange {
  end: number
  start: number
}

function parseByteRange(value: null | string, size: number): ByteRange | null | undefined {
  if (!value) {
    return null
  }

  const match = /^bytes=(\d*)-(\d*)$/.exec(value.trim())

  if (!match || size <= 0) {
    return undefined
  }

  const [, rawStart, rawEnd] = match

  if (!rawStart && !rawEnd) {
    return undefined
  }

  if (!rawStart) {
    const suffixLength = Number(rawEnd)

    if (!Number.isSafeInteger(suffixLength) || suffixLength <= 0) {
      return undefined
    }

    return { end: size - 1, start: Math.max(0, size - suffixLength) }
  }

  const start = Number(rawStart)
  const requestedEnd = rawEnd ? Number(rawEnd) : size - 1

  if (
    !Number.isSafeInteger(start)
    || !Number.isSafeInteger(requestedEnd)
    || start < 0
    || start >= size
    || requestedEnd < start
  ) {
    return undefined
  }

  return { end: Math.min(requestedEnd, size - 1), start }
}

export async function localMediaResponse(
  filePath: string,
  requestHeaders: Headers,
  method: MediaRequestMethod
): Promise<Response> {
  const file = await stat(filePath)

  if (!file.isFile()) {
    throw new Error('Media path is not a file')
  }

  const range = parseByteRange(requestHeaders.get('range'), file.size)
  const contentType = MEDIA_CONTENT_TYPES[path.extname(filePath).toLowerCase()] ?? 'application/octet-stream'

  if (range === undefined) {
    return new Response(null, {
      headers: {
        'accept-ranges': 'bytes',
        'content-range': `bytes */${file.size}`,
        'content-type': contentType
      },
      status: 416
    })
  }

  const start = range?.start ?? 0
  const end = range?.end ?? Math.max(0, file.size - 1)

  const headers = new Headers({
    'accept-ranges': 'bytes',
    'content-length': String(file.size ? end - start + 1 : 0),
    'content-type': contentType
  })

  if (range) {
    headers.set('content-range', `bytes ${start}-${end}/${file.size}`)
  }

  const body = method === 'HEAD' || file.size === 0
    ? null
    : Readable.toWeb(createReadStream(filePath, { end, start })) as unknown as BodyInit

  return new Response(body, { headers, status: range ? 206 : 200 })
}

export function createMediaProtocolHandler(dependencies: MediaProtocolDependencies) {
  return async (request: Pick<Request, 'headers' | 'method' | 'url'>): Promise<Response> => {
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('Method not allowed', {
        headers: { allow: 'GET, HEAD' },
        status: 405
      })
    }

    const method: MediaRequestMethod = request.method
    let target: MediaProtocolTarget

    try {
      target = parseMediaProtocolTarget(request.url)
    } catch {
      return new Response('Media not found', { status: 404 })
    }

    if (!isStreamableMediaPath(target.filePath)) {
      return new Response('Unsupported media type', { status: 415 })
    }

    const headers = mediaRequestHeaders(request.headers)

    if (target.mode === 'stream') {
      try {
        const resolvedPath = await dependencies.resolveLocalFile(target.filePath)

        if (!isStreamableMediaPath(resolvedPath)) {
          return new Response('Unsupported media type', { status: 415 })
        }

        return await dependencies.fetchLocal(resolvedPath, headers, method)
      } catch {
        return new Response('Media not found', { status: 404 })
      }
    }

    try {
      const connection = await dependencies.resolveRemoteConnection(target.profile)

      if (connection.mode !== 'remote') {
        return new Response('Remote media backend unavailable', { status: 404 })
      }

      const endpoint = remoteMediaEndpoint(connection.baseUrl, target.filePath)

      if (connection.authMode === 'oauth') {
        const bearer = await dependencies.ensureRemoteBearer(connection.baseUrl)

        if (bearer) {
          headers.set('authorization', `Bearer ${bearer}`)

          return await dependencies.fetchRemote(endpoint, headers, method)
        }

        return await dependencies.fetchRemoteWithCookies(endpoint, headers, method)
      }

      if (!connection.token) {
        return new Response('Remote media authentication unavailable', { status: 401 })
      }

      headers.set('x-hermes-session-token', connection.token)

      return await dependencies.fetchRemote(endpoint, headers, method)
    } catch {
      return new Response('Remote media unavailable', { status: 502 })
    }
  }
}
