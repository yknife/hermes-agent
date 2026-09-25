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
  WikiAnswer,
  WikiBackfillPreview,
  WikiCatalog,
  WikiCitationTarget,
  WikiDiff,
  WikiIngestion,
  WikiPage,
  WikiRevision,
  WikiSavedAnswer,
  WikiSearchResult,
  WikiSemanticLint,
  WikiSettings,
  WikiSourceSnapshot,
  WikiStructureLint
} from './types'

type Rest = <T>(path: string, opts?: PluginRestOptions) => Promise<T>
type Socket = (path: string, onMessage: (data: unknown) => void) => () => void
let rest: null | Rest = null

export function bindApi(value: Rest, socket?: Socket): () => void {
  rest = value

  let refreshTimer: ReturnType<typeof setTimeout> | null = null
  let refreshAll = false

  const flushRefresh = (): void => {
    refreshTimer = null

    if (refreshAll) {
      refreshAll = false
      void queryClient.invalidateQueries({ queryKey: ['video-knowledge'] })

      return
    }

    // Progress can arrive many times per second. Only task views need it;
    // refetching the whole media and Wiki library for every event can exhaust
    // the backend's SQLite connection pool during an import.
    void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'jobs'] })
    void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'job-events'] })
  }

  const stopEvents = socket?.('/events', data => {
    if (typeof data === 'object' && data !== null && 'type' in data && data.type !== 'system.heartbeat') {
      if (data.type !== 'job.progress') {refreshAll = true}

      if (refreshTimer === null) {refreshTimer = setTimeout(flushRefresh, 750)}
    }
  })

  return () => {
    if (refreshTimer !== null) {clearTimeout(refreshTimer)}
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
export const askWiki = (question: string) =>
  call<WikiAnswer>('/wiki/query', { method: 'POST', body: { question }, timeoutMs: 180_000 })
export const saveWikiAnswer = (runId: string) =>
  call<WikiSavedAnswer>(`/wiki/query/${encodeURIComponent(runId)}/save`, { method: 'POST', timeoutMs: 30_000 })
export const lintWikiStructure = () => call<WikiStructureLint>('/wiki/lint/structure')
export const lintWikiSemantics = (focus = 'all pages') =>
  call<WikiSemanticLint>('/wiki/lint/semantic', { method: 'POST', body: { focus }, timeoutMs: 180_000 })
export const fetchWikiHistory = (pageId: string) =>
  call<WikiRevision[]>(`/wiki/pages/${encodeURIComponent(pageId)}/history`)
export const fetchWikiDiff = (pageId: string, revision?: number) =>
  call<WikiDiff>(`/wiki/pages/${encodeURIComponent(pageId)}/diff${revision ? `?revision=${revision}` : ''}`)
export const rollbackWikiPage = (pageId: string, revision: number) =>
  call<{ commit_id: string; revision: number }>(`/wiki/pages/${encodeURIComponent(pageId)}/rollback`, {
    method: 'POST', body: { revision }
  })
export const repairWikiLinks = (pageId: string) =>
  call<{ page_id: string; repaired: number; commit_id?: string }>(
    `/wiki/pages/${encodeURIComponent(pageId)}/repair-links`, { method: 'POST' }
  )
export const applyWikiReview = (pageId: string, expectedRevision: number, body: string, lintRunId: string) =>
  call<{ page_id: string; revision: number; commit_id: string }>(
    `/wiki/pages/${encodeURIComponent(pageId)}/review`, {
      method: 'POST', body: { expected_revision: expectedRevision, body, lint_run_id: lintRunId }
    }
  )
export const repairWikiIndex = () =>
  call<{ commit_id: string; wiki_revision: number }>('/wiki/maintenance/index/repair', { method: 'POST' })
export const previewWikiSchema = () =>
  call<{ schema_sha256: string; schema_version: number; affected_page_ids: string[]; outdated_page_ids: string[]; count: number }>('/wiki/maintenance/schema/preview')
export const withdrawWikiSource = (mediaId: string, revision: string, reason: string) =>
  call<{ commit_id: string; affected_page_ids: string[] }>(
    `/wiki/sources/${encodeURIComponent(mediaId)}/${encodeURIComponent(revision)}/withdraw`,
    { method: 'POST', body: { reason } }
  )
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
export const submitWikiFusionBackfill = (mediaIds?: string[]) =>
  call<{ job_ids: string[] }>('/wiki/fusion/backfill', {
    method: 'POST', body: { media_ids: mediaIds ?? null }
  })
export const recompileWikiFusion = (mediaIds?: string[]) =>
  call<{ job_ids: string[] }>('/wiki/fusion/recompile', {
    method: 'POST', body: { media_ids: mediaIds ?? null }
  })
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
