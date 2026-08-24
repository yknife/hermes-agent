import {
  Badge,
  Button,
  Codicon,
  EmptyState,
  Loader,
  ScrollArea,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useState } from 'react'

import { fetchJobEvents, fetchJobs, fetchLiveSources, fetchMedia, jobAction } from './api'
import { beijingDateTime, errorMessage } from './format'
import { collapseProgressEvents } from './job-events'
import type { Job, JobStatus, LiveSource, Media } from './types'

const STATUS_LABELS: Record<JobStatus, string> = {
  CANCELLED: '已取消',
  FAILED: '失败',
  PARTIAL: '部分完成',
  PAUSED: '已暂停',
  PENDING: '等待领取',
  RETRY_WAIT: '等待重试',
  RUNNING: '运行中',
  SUCCEEDED: '已完成',
  WAITING_LIVE: '等待直播'
}

export function JobsView({ initialJobId }: { initialJobId?: null | string }) {
  const [filter, setFilter] = useState<'ALL' | JobStatus>('ALL')
  const [scope, setScope] = useState<'all' | 'today'>(initialJobId ? 'all' : 'today')
  const [selected, setSelected] = useState<null | string>(initialJobId ?? null)

  const jobs = useQuery({
    queryFn: () => fetchJobs(filter === 'ALL' ? undefined : filter, scope),
    queryKey: ['video-knowledge', 'jobs', filter, scope],
    refetchInterval: 2_000
  })

  const media = useQuery({
    queryFn: fetchMedia,
    queryKey: ['video-knowledge', 'media'],
    refetchInterval: 5_000
  })

  const liveSources = useQuery({
    queryFn: fetchLiveSources,
    queryKey: ['video-knowledge', 'sources', 'live'],
    refetchInterval: 5_000
  })

  const selectedIsVisible = jobs.data?.items.some(job => job.id === selected) ?? false
  const activeJobId = selectedIsVisible ? selected : jobs.data?.items[0]?.id ?? null

  const events = useQuery({
    enabled: Boolean(activeJobId),
    queryFn: () => fetchJobEvents(activeJobId!),
    queryKey: ['video-knowledge', 'job-events', activeJobId],
    refetchInterval: 2_000
  })

  return (
    <div className="grid min-h-0 flex-1 grid-cols-[minmax(28rem,1.45fr)_minmax(18rem,0.75fr)]">
      <section className="min-h-0 border-r border-(--ui-stroke-secondary)">
        <div className="flex items-center justify-between gap-4 border-b border-(--ui-stroke-secondary) px-5 py-3">
          <div>
            <h2 className="text-sm font-semibold">任务中心</h2>
            <p className="mt-0.5 text-xs text-muted-foreground">状态由 SQLite 持久化，页面定时同步权威状态。</p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Select onValueChange={value => setScope(value as typeof scope)} value={scope}>
              <SelectTrigger className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="today">今日任务</SelectItem>
                <SelectItem value="all">全部历史</SelectItem>
              </SelectContent>
            </Select>
            <Select onValueChange={value => setFilter(value as typeof filter)} value={filter}>
              <SelectTrigger className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="ALL">全部状态</SelectItem>
                {Object.entries(STATUS_LABELS).map(([value, label]) => (
                  <SelectItem key={value} value={value}>
                    {label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        <ScrollArea className="h-[calc(100%-4rem)] p-4">
          {jobs.isLoading ? (
            <Loader className="mx-auto mt-12" />
          ) : !jobs.data?.items.length ? (
            <EmptyState description="从“添加内容”创建一个视频任务。" title="当前筛选下没有任务" />
          ) : (
            jobs.data.items.map(job => {
              const owner = jobOwnerLabel(job, media.data ?? [], liveSources.data ?? [])

              return <article
                className={`mb-3 rounded-lg border p-4 ${activeJobId === job.id ? 'border-primary/50 bg-primary/5' : 'border-(--ui-stroke-secondary) bg-(--ui-bg-secondary)'}`}
                key={job.id}
                onClick={() => setSelected(job.id)}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <strong className="text-sm">{job.type}</strong>
                      <StatusBadge status={job.status} />
                    </div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      {job.stage} · 尝试 {job.attempt_count}/{job.max_attempts}
                    </div>
                    <div className="mt-1 flex min-w-0 items-center gap-1.5 text-xs text-(--ui-text-secondary)" title={owner}>
                      <Codicon className="shrink-0" name={job.type === 'RECORD_LIVE' ? 'radio-tower' : 'video'} />
                      <span className="truncate">归属：{owner}</span>
                    </div>
                    <code className="mt-1 block truncate text-[0.65rem] text-muted-foreground">{job.id}</code>
                  </div>
                  <strong className="text-sm tabular-nums">{job.progress.toFixed(0)}%</strong>
                </div>
                <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-(--ui-bg-quaternary)">
                  <div
                    className="h-full rounded-full bg-primary transition-[width]"
                    style={{ width: `${job.progress}%` }}
                  />
                </div>
                {job.error_message && (
                  <div className="mt-3 rounded bg-destructive/10 px-3 py-2 text-xs text-destructive">
                    <strong>{job.error_code ?? 'FAILED'}：</strong>
                    {job.error_message}
                  </div>
                )}
                <JobActions job={job} />
              </article>
            })
          )}
        </ScrollArea>
      </section>

      <aside className="min-h-0">
        <div className="border-b border-(--ui-stroke-secondary) px-4 py-3">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            任务事件（北京时间）
          </h2>
        </div>
        <ScrollArea className="h-[calc(100%-2.75rem)] p-4">
          {!activeJobId ? (
            <EmptyState title="选择任务查看事件" />
          ) : events.isLoading ? (
            <Loader className="mx-auto mt-12" />
          ) : !events.data?.length ? (
            <EmptyState title="暂无事件" />
          ) : (
            collapseProgressEvents(events.data)
              .reverse()
              .map(event => (
              <div
                className="relative border-l border-(--ui-stroke-secondary) pb-4 pl-4 text-xs last:pb-0"
                key={event.event_id}
              >
                <span className="absolute -left-1 top-1 size-2 rounded-full bg-primary" />
                <div className="font-medium">{event.data.message ?? event.type}</div>
                <div className="mt-1 text-[0.6875rem] text-muted-foreground">
                  {event.data.stage ?? '—'} · {event.data.overall_progress?.toFixed(0) ?? 0}%
                </div>
                <time
                  className="mt-1 block text-[0.65rem] text-muted-foreground"
                  dateTime={event.occurred_at}
                  title="北京时间（UTC+8）"
                >
                  {beijingDateTime(event.occurred_at)}
                </time>
              </div>
              ))
          )}
        </ScrollArea>
      </aside>
    </div>
  )
}

function jobOwnerLabel(job: Job, media: Media[], liveSources: LiveSource[]): string {
  const inputMediaId = typeof job.input.media_id === 'string' ? job.input.media_id : null
  const resultMediaId = typeof job.result?.media_id === 'string' ? job.result.media_id : null
  const mediaId = job.media_id ?? inputMediaId ?? resultMediaId

  const item = media.find(value => value.id === mediaId)
    ?? media.find(value => value.source_id === job.source_id)

  if (item) {
    return item.title
  }

  const liveSource = liveSources.find(value => value.id === job.source_id)

  if (liveSource) {
    return liveSource.title ?? `${liveSource.platform} 直播 · ${liveSource.url}`
  }

  if (typeof job.input.url === 'string') {
    return job.input.url
  }

  return '尚未关联媒体'
}

function StatusBadge({ status }: { status: JobStatus }) {
  const variant = status === 'FAILED' ? 'destructive' : status === 'SUCCEEDED' ? 'default' : 'outline'

  return <Badge variant={variant}>{STATUS_LABELS[status]}</Badge>
}

function JobActions({ job }: { job: Job }) {
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: (action: 'cancel' | 'pause' | 'resume' | 'retry') => jobAction(job.id, action),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['video-knowledge'] })
  })

  const actions: Array<{ action: 'cancel' | 'pause' | 'resume' | 'retry'; label: string }> = []

  if (job.status === 'RUNNING') {
    actions.push({ action: 'pause', label: '暂停' }, { action: 'cancel', label: '取消' })
  }

  if (['PENDING', 'WAITING_LIVE', 'RETRY_WAIT'].includes(job.status)) {
    actions.push({ action: 'cancel', label: '取消' })
  }

  if (job.status === 'PAUSED') {
    actions.push({ action: 'resume', label: '恢复' }, { action: 'cancel', label: '取消' })
  }

  if (['FAILED', 'PARTIAL', 'CANCELLED'].includes(job.status)) {
    actions.push({ action: 'retry', label: '重试' })
  }

  if (!actions.length) {
    return null
  }

  return (
    <div className="mt-3 flex items-center gap-2" onClick={event => event.stopPropagation()}>
      {actions.map(item => (
        <Button
          disabled={mutation.isPending}
          key={item.action}
          onClick={() => mutation.mutate(item.action)}
          size="xs"
          variant="secondary"
        >
          {item.label}
        </Button>
      ))}
      {mutation.error && <span className="text-xs text-destructive">{errorMessage(mutation.error)}</span>}
    </div>
  )
}
