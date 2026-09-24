import { type PluginRestOptions, queryClient } from '@hermes/plugin-sdk'

import type {
  AsrSettingsUpdate,
  AsrStatus,
  CookiePlatform,
  CookieSettings,
  Health,
  IngestOptions,
  IngestResult,
  Job,
  JobEvent,
  JobList,
  JobStatus,
  KnowledgeDocument,
  LiveSource,
  LiveSourceCreateResult,
  LiveSourceOptions,
  LocalIngestOptions,
  Media,
  MediaDeleteResult,
  MessagingQuotaSettings,
  PlaybackInfo,
  Probe,
  RuntimeStatus,
  StorageSettings,
  Transcript,
  TranscriptSearchResult,
  WikiBackfillPreview,
  WikiCatalog,
  WikiCitationTarget,
  WikiIngestion,
  WikiPage,
  WikiSearchResult,
  WikiSettings,
  WikiSourceSnapshot
} from './types'

type Rest = <T>(path: string, opts?: PluginRestOptions) => Promise<T>
type Socket = (path: string, onMessage: (data: unknown) => void) => () => void
let rest: null | Rest = null

export function bindApi(value: Rest, socket?: Socket): () => void {
  rest = value

  const stopEvents = socket?.('/events', data => {
    if (typeof data === 'object' && data !== null && 'type' in data && data.type !== 'system.heartbeat') {
      void queryClient.invalidateQueries({ queryKey: ['video-knowledge'] })
    }
  })

  return () => {
    stopEvents?.()
    rest = null
  }
}

function call<T>(path: string, options?: PluginRestOptions): Promise<T> {
  if (!rest) {
    throw new Error('Video Knowledge API is not ready')
  }

  return rest<T>(path, options)
}

export const fetchHealth = () => call<Health>('/system/health')
export const fetchRuntimeStatus = () => call<RuntimeStatus>('/system/runtime')
export const fetchStorageSettings = () => call<StorageSettings>('/system/storage')
export const fetchCookieSettings = () => call<CookieSettings>('/system/cookies')
export const updateCookieSettings = (platform: CookiePlatform, cookiesFile: null | string) =>
  call<CookieSettings>(`/system/cookies/${encodeURIComponent(platform)}`, {
    method: 'PUT',
    body: { cookies_file: cookiesFile }
  })
export const fetchMessagingQuotaSettings = () => call<MessagingQuotaSettings>('/system/messaging-quotas')
export const updateMessagingQuotaSettings = (value: MessagingQuotaSettings) =>
  call<MessagingQuotaSettings>('/system/messaging-quotas', {
    method: 'PUT',
    body: value
  })
export const migrateStorage = (targetPath: string) =>
  call<StorageSettings>('/system/storage', {
    method: 'PUT',
    body: { target_path: targetPath },
    timeoutMs: 30_000
  })
export const fetchAsrStatus = () => call<AsrStatus>('/system/asr')
export const updateAsrSettings = (value: AsrSettingsUpdate) =>
  call<AsrStatus>('/system/asr', { method: 'PUT', body: value })
export const downloadAsrModel = (model: string) =>
  call<AsrStatus>(`/system/asr/models/${encodeURIComponent(model)}/download`, {
    method: 'POST',
    timeoutMs: 30 * 60 * 1000
  })
export const fetchJobs = (status?: JobStatus, scope: 'all' | 'today' = 'all') =>
  call<JobList>(`/jobs?limit=100&scope=${scope}${status ? `&status=${encodeURIComponent(status)}` : ''}`)
export const fetchJobEvents = (jobId: string) => call<JobEvent[]>(`/jobs/${encodeURIComponent(jobId)}/events`)
export const jobAction = (jobId: string, action: 'cancel' | 'pause' | 'resume' | 'retry') =>
  call<Job>(`/jobs/${encodeURIComponent(jobId)}/${action}`, { method: 'POST' })
export const fetchMedia = () => call<Media[]>('/media')
export const fetchMediaItem = (mediaId: string) => call<Media>(`/media/${encodeURIComponent(mediaId)}`)
export const deleteMedia = (mediaId: string) =>
  call<MediaDeleteResult>(`/media/${encodeURIComponent(mediaId)}`, { method: 'DELETE' })
export const fetchPlayback = (mediaId: string) => call<PlaybackInfo>(`/media/${encodeURIComponent(mediaId)}/playback`)
export const fetchTranscript = (mediaId: string) => call<Transcript>(`/media/${encodeURIComponent(mediaId)}/transcript`)
export const createTranscript = (mediaId: string) =>
  call<Job>(`/media/${encodeURIComponent(mediaId)}/transcript`, { method: 'POST' })
export const searchTranscript = (mediaId: string, query: string) =>
  call<TranscriptSearchResult[]>(`/search?media_id=${encodeURIComponent(mediaId)}&q=${encodeURIComponent(query)}`)
export const fetchKnowledge = (mediaId: string) =>
  call<KnowledgeDocument[]>(`/media/${encodeURIComponent(mediaId)}/knowledge`)
export const probeSource = (url: string, cookiesFile: null | string = null) =>
  call<Probe>('/sources/probe', {
    method: 'POST',
    body: { url, cookies_file: cookiesFile },
    timeoutMs: 45_000
  })
export const ingest = (url: string, options: IngestOptions) =>
  call<IngestResult>('/sources/ingest', {
    method: 'POST',
    body: { url, ...options },
    timeoutMs: 30_000
  })
export const ingestLocal = (path: string, title: string, author: string, options: LocalIngestOptions) =>
  call<IngestResult>('/sources/local', {
    method: 'POST',
    body: { path, title, author: author.trim() || null, ...options },
    timeoutMs: 30_000
  })
export const fetchLiveSources = () => call<LiveSource[]>('/sources/live')
export const createLiveSource = (url: string, options: LiveSourceOptions) =>
  call<LiveSourceCreateResult>('/sources/live', {
    method: 'POST',
    body: { url, ...options },
    timeoutMs: 30_000
  })
export const updateLiveSource = (sourceId: string, enabled: boolean) =>
  call<LiveSource>(`/sources/${encodeURIComponent(sourceId)}`, {
    method: 'PATCH',
    body: { enabled }
  })
export const checkLiveSource = (sourceId: string) =>
  call<Job>(`/sources/${encodeURIComponent(sourceId)}/check-live`, { method: 'POST' })
export const analyze = (mediaId: string, selection: null | { model: string; provider: string } = null) =>
  call<Job>(`/media/${encodeURIComponent(mediaId)}/analyze`, {
    method: 'POST',
    body: {
      force: true,
      analysis_model: selection?.model ?? null,
      analysis_provider: selection?.provider ?? null
    }
  })

export const fetchWikiCatalog = (pageType?: string, tag?: string) =>
  call<WikiCatalog>(
    `/wiki/pages?${new URLSearchParams({ ...(pageType ? { page_type: pageType } : {}), ...(tag ? { tag } : {}) })}`
  )
export const fetchWikiPage = (pageId: string) => call<WikiPage>(`/wiki/pages/${encodeURIComponent(pageId)}`)
export const searchWiki = (query: string, pageType?: string, tag?: string) =>
  call<WikiSearchResult>(
    `/wiki/search?${new URLSearchParams({ q: query, ...(pageType ? { page_type: pageType } : {}), ...(tag ? { tag } : {}) })}`
  )
export const rebuildWikiSearch = () =>
  call<{ count: number; initialized: boolean; revision: number }>('/wiki/search/rebuild', { method: 'POST' })
export const resolveWikiCitation = (pageId: string, itemKey: string) =>
  call<WikiCitationTarget>(`/wiki/pages/${encodeURIComponent(pageId)}/citations/${encodeURIComponent(itemKey)}`)
export const fetchWikiSource = (mediaId: string, revision: string) =>
  call<WikiSourceSnapshot>(`/wiki/sources/${encodeURIComponent(mediaId)}/${encodeURIComponent(revision)}`)
export const fetchWikiSettings = () => call<WikiSettings>('/wiki/settings')
export const updateWikiSettings = (autoIngest: boolean) =>
  call<WikiSettings>('/wiki/settings', { method: 'PUT', body: { auto_ingest: autoIngest } })
export const fetchWikiIngestions = (mediaId?: string) =>
  call<WikiIngestion[]>(`/wiki/ingestions${mediaId ? `?media_id=${encodeURIComponent(mediaId)}` : ''}`)
export const submitWikiMedia = (mediaId: string) =>
  call<{ ingestion_id: string; job_id: string }>(`/wiki/media/${encodeURIComponent(mediaId)}/ingest`, {
    method: 'POST'
  })
export const previewWikiBackfill = () =>
  call<WikiBackfillPreview[]>('/wiki/backfill/preview', { method: 'POST', body: {} })
export const submitWikiBackfill = () =>
  call<{ batch_id: string; job_ids: string[] }>('/wiki/backfill', { method: 'POST', body: {} })
export const cancelWikiBackfill = (batchId: string) =>
  call<{ batch_id: string; cancelled_job_ids: string[] }>(`/wiki/backfill/${encodeURIComponent(batchId)}/cancel`, {
    method: 'POST'
  })

export function mediaPlaybackUrl(path: string): string {
  return `hermes-media://stream/${encodeURIComponent(path)}`
}

export function mediaThumbnailUrl(value: string): string {
  return /^(?:https?:|data:|blob:)/i.test(value) ? value : mediaPlaybackUrl(value)
}
