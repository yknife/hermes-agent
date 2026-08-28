import { afterEach, describe, expect, it, vi } from 'vitest'

import { bindApi, deleteMedia, fetchRuntimeStatus, ingest, mediaPlaybackUrl, mediaThumbnailUrl, probeSource } from './api'
import type { IngestOptions } from './types'

const dispose: Array<() => void> = []

afterEach(() => {
  dispose.splice(0).forEach(run => run())
})

describe('video knowledge plugin API', () => {
  it('subscribes to the persisted event stream when the host provides sockets', () => {
    const rest = vi.fn().mockResolvedValue({})
    const stop = vi.fn()
    const socket = vi.fn().mockReturnValue(stop)

    const unbind = bindApi(rest, socket)

    expect(socket).toHaveBeenCalledWith('/events', expect.any(Function))
    unbind()
    expect(stop).toHaveBeenCalledOnce()
  })

  it('keeps source probing and ingest inside the plugin REST namespace', async () => {
    const rest = vi.fn().mockResolvedValue({})

    dispose.push(bindApi(rest))
    await probeSource('https://example.test/video')

    const options: IngestOptions = {
      asr_compute_type: 'auto',
      asr_device: 'cuda',
      asr_enabled: true,
      asr_language: 'zh',
      asr_model: 'medium',
      asr_vad_filter: true,
      asr_word_timestamps: true,
      auto_analyze: true,
      max_height: 1440,
      subtitle_languages: ['zh-CN', 'en']
    }

    await ingest('https://example.test/video', options)

    expect(rest).toHaveBeenNthCalledWith(1, '/sources/probe', {
      body: { url: 'https://example.test/video' },
      method: 'POST',
      timeoutMs: 45_000
    })
    expect(rest).toHaveBeenNthCalledWith(2, '/sources/ingest', {
      body: { url: 'https://example.test/video', ...options },
      method: 'POST',
      timeoutMs: 30_000
    })
  })

  it('encodes Windows paths into the seekable Hermes media protocol', () => {
    expect(mediaPlaybackUrl(String.raw`C:\Hermes Data\video-knowledge\source.mp4`)).toBe(
      'hermes-media://stream/C%3A%5CHermes%20Data%5Cvideo-knowledge%5Csource.mp4'
    )
  })

  it('uses the media protocol only for local thumbnail paths', () => {
    expect(mediaThumbnailUrl(String.raw`C:\Hermes Data\thumbnail.jpg`)).toBe(
      'hermes-media://stream/C%3A%5CHermes%20Data%5Cthumbnail.jpg'
    )
    expect(mediaThumbnailUrl('https://example.test/thumbnail.jpg')).toBe(
      'https://example.test/thumbnail.jpg'
    )
  })

  it('loads release runtime readiness from the plugin namespace', async () => {
    const rest = vi.fn().mockResolvedValue({ ready: true, tools: [] })

    dispose.push(bindApi(rest))
    await fetchRuntimeStatus()

    expect(rest).toHaveBeenCalledWith('/system/runtime', undefined)
  })

  it('deletes media through the plugin REST namespace', async () => {
    const rest = vi.fn().mockResolvedValue({ media_id: 'media/one' })

    dispose.push(bindApi(rest))
    await deleteMedia('media/one')

    expect(rest).toHaveBeenCalledWith('/media/media%2Fone', { method: 'DELETE' })
  })
})
