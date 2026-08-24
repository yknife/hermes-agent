export type JobStatus =
  'PENDING' | 'RUNNING' | 'WAITING_LIVE' | 'RETRY_WAIT' | 'PAUSED' | 'SUCCEEDED' | 'PARTIAL' | 'FAILED' | 'CANCELLED'

export interface Health {
  status: 'ok' | 'degraded'
  components: Record<string, { detail?: null | string; status: string }>
}

export interface AsrStatus {
  enabled: boolean
  model: string
  configured_device: string
  effective_device: string
  configured_compute_type: string
  effective_compute_type: string
  cuda_available: boolean
  language: null | string
  vad_filter: boolean
  word_timestamps: boolean
  chunk_seconds: number
  overlap_seconds: number
  auto_analyze: boolean
  models: AsrModelStatus[]
}

export interface AsrModelStatus {
  name: string
  size: string
  description: string
  downloaded: boolean
  downloading: boolean
}

export interface AsrSettingsUpdate {
  enabled: boolean
  model: string
  configured_device: 'auto' | 'cpu' | 'cuda'
  configured_compute_type: 'auto' | 'int8' | 'float16' | 'float32'
  language: null | string
  vad_filter: boolean
  word_timestamps: boolean
  chunk_seconds: number
  overlap_seconds: number
  auto_analyze: boolean
}

export interface AsrOptions {
  asr_enabled: boolean
  asr_model: string
  asr_device: 'auto' | 'cpu' | 'cuda'
  asr_compute_type: string
  asr_language: null | string
  asr_vad_filter: boolean
  asr_word_timestamps: boolean
}

export interface Job {
  id: string
  source_id: null | string
  media_id: null | string
  type: string
  status: JobStatus
  stage: string
  priority: number
  progress: number
  attempt_count: number
  max_attempts: number
  next_run_at: string
  lease_owner: null | string
  lease_expires_at: null | string
  cancel_requested_at: null | string
  input: Record<string, unknown>
  result: null | Record<string, unknown>
  error_code: null | string
  error_message: null | string
  created_at: string
  updated_at: string
}

export interface JobEvent {
  event_id: string
  type: string
  occurred_at: string
  data: {
    actor?: string
    from_status?: null | string
    job_id: string
    message?: null | string
    overall_progress?: number
    sequence?: number
    stage?: string
    status?: null | string
  }
}

export interface JobList {
  items: Job[]
  next_cursor?: null | string
}

export interface Probe {
  source_type: 'LIVE' | 'VIDEO'
  external_id: string
  title: string
  webpage_url: string
  platform: string
  author: null | string
  thumbnail_url: null | string
  duration_seconds: null | number
  is_live: boolean
  subtitles: Array<{ automatic: boolean; formats: string[]; language: string }>
}

export interface Source {
  id: string
  type: string
  platform: string
  url: string
  canonical_url: string
  external_id: null | string
  title: null | string
  enabled: boolean
  created_at: string
}

export interface LiveSession {
  id: string
  title: null | string
  anchor: null | string
  status: string
  media_id: null | string
  started_at: null | string
  ended_at: null | string
}

export interface LiveSource {
  id: string
  platform: string
  url: string
  title: null | string
  enabled: boolean
  poll_interval_seconds: number
  quality_policy: string
  recording_max_seconds: number
  last_checked_at: null | string
  next_check_at: null | string
  monitor_job: Job | null
  latest_session: LiveSession | null
  created_at: string
}

export interface LiveSourceCreateResult {
  source: LiveSource
  job: Job
  duplicate: boolean
}

export interface LiveSourceOptions extends AsrOptions {
  auto_analyze: boolean
  poll_interval_seconds: number
  quality_policy: 'HD' | 'LD' | 'OD' | 'SD' | 'UHD'
  reconnect_attempts: number
  reconnect_delay_seconds: number
  recording_max_seconds: number
}

export interface MediaAsset {
  id: string
  kind: string
  relative_path: string
  mime_type: null | string
  container: null | string
  codec: null | string
  size_bytes: number
  duration_seconds: null | number
  sha256: string
  status: string
}

export interface Media {
  id: string
  source_id: string
  external_id: string
  title: string
  author: null | string
  description: null | string
  webpage_url: string
  thumbnail_url: null | string
  duration_seconds: null | number
  published_at: null | string
  metadata: Record<string, unknown>
  created_at: string
  assets: MediaAsset[]
}

export interface IngestResult {
  source: Source
  job: Job
  media: Media | null
  duplicate: boolean
}

export interface PlaybackInfo {
  path: string
  mime_type: string
}

export interface TranscriptSegment {
  id: string
  index: number
  start_ms: number
  end_ms: number
  speaker: null | string
  text: string
  confidence: null | number
}

export interface Transcript {
  id: string
  media_id: string
  version: number
  language: string
  source_type: string
  status: string
  created_at: string
  segments: TranscriptSegment[]
}

export interface TranscriptSearchResult {
  media_id: string
  segment: TranscriptSegment
}

export interface KnowledgeDocument {
  id: string
  document_type: string
  version: number
  content: unknown
  model: string
}

export interface IngestOptions extends AsrOptions {
  auto_analyze: boolean
  max_height: number
  subtitle_languages: string[]
}
