import {
  Badge,
  Button,
  Codicon,
  EmptyState,
  host,
  Input,
  Loader,
  ScrollArea,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useMemo, useRef, useState } from 'react'

import {
  analyze,
  createTranscript,
  fetchJobs,
  fetchKnowledge,
  fetchMedia,
  fetchPlayback,
  fetchTranscript,
  mediaPlaybackUrl,
  mediaThumbnailUrl,
  searchTranscript
} from './api'
import { stageLibraryChatContext, stageMediaChatContext } from './chat-context'
import { durationLabel, errorMessage, fileSize, readableContent, timestamp } from './format'
import { buildKnowledgeTimeline } from './knowledge-timeline'

export function LibraryView({
  initialMediaId,
  initialSeekMs
}: {
  initialMediaId?: null | string
  initialSeekMs?: null | number
}) {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<null | string>(initialMediaId ?? null)
  const [query, setQuery] = useState('')
  const [currentMs, setCurrentMs] = useState(0)
  const [knowledgeView, setKnowledgeView] = useState<'categories' | 'timeline'>('timeline')
  const player = useRef<HTMLVideoElement>(null)
  const media = useQuery({ queryKey: ['video-knowledge', 'media'], queryFn: fetchMedia, refetchInterval: 5_000 })
  const activeMediaId = selected ?? media.data?.[0]?.id ?? null
  const activeMedia = media.data?.find(item => item.id === activeMediaId) ?? null

  const playback = useQuery({
    enabled: Boolean(activeMediaId),
    queryFn: () => fetchPlayback(activeMediaId!),
    queryKey: ['video-knowledge', 'playback', activeMediaId],
    retry: false
  })

  const transcript = useQuery({
    enabled: Boolean(activeMediaId),
    queryFn: () => fetchTranscript(activeMediaId!),
    queryKey: ['video-knowledge', 'transcript', activeMediaId],
    refetchInterval: 5_000,
    refetchIntervalInBackground: true,
    retry: 3
  })

  const search = useQuery({
    enabled: Boolean(activeMediaId && query.trim()),
    queryFn: () => searchTranscript(activeMediaId!, query.trim()),
    queryKey: ['video-knowledge', 'transcript-search', activeMediaId, query],
    retry: false
  })

  const knowledge = useQuery({
    enabled: Boolean(activeMediaId),
    queryFn: () => fetchKnowledge(activeMediaId!),
    queryKey: ['video-knowledge', 'knowledge', activeMediaId],
    refetchInterval: 5_000
  })

  const jobs = useQuery({
    queryFn: () => fetchJobs(),
    queryKey: ['video-knowledge', 'jobs'],
    refetchInterval: 5_000
  })

  const latestAnalysis = jobs.data?.items.find(
    item => item.type === 'ANALYZE' && item.input.media_id === activeMediaId
  )

  const transcriptJob = useMutation({
    mutationFn: () => createTranscript(activeMediaId!),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['video-knowledge'] })
  })

  const analysisJob = useMutation({
    mutationFn: () => analyze(activeMediaId!),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['video-knowledge'] })
  })

  const visibleSegments = useMemo(() => {
    if (!transcript.data) {
      return []
    }

    if (!query.trim()) {
      return transcript.data.segments
    }

    const ids = new Set(search.data?.map(item => item.segment.id) ?? [])

    return transcript.data.segments.filter(segment => ids.has(segment.id))
  }, [query, search.data, transcript.data])

  const knowledgeTimeline = useMemo(
    () => buildKnowledgeTimeline(knowledge.data ?? []),
    [knowledge.data]
  )

  const seek = (milliseconds: number) => {
    if (!player.current) {
      return
    }

    player.current.currentTime = milliseconds / 1000
    void player.current.play()
  }

  if (media.isLoading) {
    return <Loader className="m-auto" />
  }

  return (
    <div className="grid min-h-0 flex-1 grid-cols-[minmax(14rem,0.65fr)_minmax(28rem,1.7fr)]">
      <aside className="min-h-0 border-r border-(--ui-stroke-secondary)">
        <div className="flex items-center justify-between px-4 py-3">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">媒体库</h2>
          <div className="flex items-center gap-2">
            <Button
              disabled={!media.data?.length}
              onClick={() => {
                stageLibraryChatContext()
                host.newChat()
              }}
              size="xs"
              variant="ghost"
            >
              <Codicon name="comment-discussion" />
              问知识库
            </Button>
            <Badge variant="outline">{media.data?.length ?? 0}</Badge>
          </div>
        </div>
        <ScrollArea className="h-[calc(100%-2.75rem)]">
          {!media.data?.length ? (
            <EmptyState description="先在“添加内容”中创建视频采集任务。" title="还没有本地媒体" />
          ) : (
            media.data.map(item => (
              <button
                className={`flex w-full gap-3 border-b border-(--ui-stroke-secondary) p-3 text-left hover:bg-(--chrome-action-hover) ${activeMediaId === item.id ? 'bg-(--ui-bg-quaternary)' : ''}`}
                key={item.id}
                onClick={() => {
                  setSelected(item.id)
                  setQuery('')
                }}
                type="button"
              >
                <div className="h-12 w-20 shrink-0 overflow-hidden rounded bg-(--ui-bg-quaternary)">
                  {item.thumbnail_url ? (
                    <img
                      alt=""
                      className="h-full w-full object-cover"
                      referrerPolicy="no-referrer"
                      src={mediaThumbnailUrl(item.thumbnail_url)}
                    />
                  ) : (
                    <Codicon className="m-4 opacity-50" name="video" />
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="line-clamp-2 text-xs font-medium leading-4">{item.title}</div>
                  <div className="mt-1 truncate text-[0.6875rem] text-muted-foreground">
                    {item.author ?? '未知作者'} · {durationLabel(item.duration_seconds)}
                  </div>
                </div>
              </button>
            ))
          )}
        </ScrollArea>
      </aside>

      {!activeMedia ? (
        <EmptyState description="选择一个已经完成采集的视频。" title="选择媒体以查看详情" />
      ) : (
        <ScrollArea className="min-h-0">
          <div className="space-y-4 p-5">
            <header className="flex items-start justify-between gap-4">
              <div className="min-w-0 flex-1">
                <div className="text-[0.6875rem] font-semibold uppercase tracking-wider text-primary">媒体详情</div>
                <h2 className="mt-1 text-lg font-semibold">{activeMedia.title}</h2>
                <p className="mt-1 text-xs text-muted-foreground">
                  {activeMedia.author ?? '未知作者'} · {durationLabel(activeMedia.duration_seconds)}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Button asChild size="xs" variant="secondary">
                  <a href={activeMedia.webpage_url} rel="noreferrer" target="_blank">
                    <Codicon name="link-external" />
                    原视频
                  </a>
                </Button>
                <Button
                  disabled={!transcript.data}
                  onClick={() => {
                    stageMediaChatContext(activeMedia.id, activeMedia.title)
                    host.newChat()
                  }}
                  size="xs"
                >
                  <Codicon name="comment-discussion" />
                  问 Hermes
                </Button>
              </div>
            </header>

            <div className="grid grid-cols-1 gap-4 2xl:grid-cols-[minmax(24rem,1.25fr)_minmax(20rem,0.9fr)]">
              <div className="space-y-4">
                <div className="overflow-hidden rounded-lg border border-(--ui-stroke-secondary) bg-black">
                  {playback.isLoading ? (
                    <div className="flex aspect-video items-center justify-center">
                      <Loader />
                    </div>
                  ) : playback.data ? (
                    <video
                      className="aspect-video w-full"
                      controls
                      onLoadedMetadata={event => {
                        if (initialSeekMs !== null && initialSeekMs !== undefined) {
                          event.currentTarget.currentTime = initialSeekMs / 1000
                          setCurrentMs(initialSeekMs)
                        }
                      }}
                      onTimeUpdate={event => setCurrentMs(event.currentTarget.currentTime * 1000)}
                      preload="metadata"
                      ref={player}
                      src={mediaPlaybackUrl(playback.data.path)}
                    />
                  ) : (
                    <div className="flex aspect-video items-center justify-center text-xs text-white/60">
                      本地视频文件不可用
                    </div>
                  )}
                </div>

                <section className="rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-4">
                  <div className="flex items-center justify-between">
                    <h3 className="text-sm font-semibold">本地资产</h3>
                    <Badge variant="outline">{activeMedia.assets.length} 个文件</Badge>
                  </div>
                  <div className="mt-3 space-y-2">
                    {activeMedia.assets.map(asset => (
                      <div
                        className="flex items-center justify-between gap-4 rounded bg-(--ui-bg-quaternary) px-3 py-2 text-xs"
                        key={asset.id}
                      >
                        <span>
                          {asset.kind} · {asset.codec ?? asset.container ?? asset.mime_type ?? '未知格式'}
                        </span>
                        <span className="text-muted-foreground">{fileSize(asset.size_bytes)}</span>
                      </div>
                    ))}
                  </div>
                  {activeMedia.description && (
                    <p className="mt-4 whitespace-pre-wrap text-xs leading-5 text-(--ui-text-secondary)">
                      {activeMedia.description}
                    </p>
                  )}
                </section>

                <section className="rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-4">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <h3 className="text-sm font-semibold">Hermes 知识结果</h3>
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        沿视频进度浏览重点内容，点击时间可跳转到对应画面。
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      <div
                        aria-label="知识结果展示方式"
                        className="flex rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-quaternary) p-0.5"
                        role="group"
                      >
                        <Button
                          aria-pressed={knowledgeView === 'timeline'}
                          onClick={() => setKnowledgeView('timeline')}
                          size="xs"
                          variant={knowledgeView === 'timeline' ? 'secondary' : 'ghost'}
                        >
                          <Codicon name="history" />
                          时间轴
                        </Button>
                        <Button
                          aria-pressed={knowledgeView === 'categories'}
                          onClick={() => setKnowledgeView('categories')}
                          size="xs"
                          variant={knowledgeView === 'categories' ? 'secondary' : 'ghost'}
                        >
                          <Codicon name="list-tree" />
                          分类视图
                        </Button>
                      </div>
                      <Button
                        disabled={analysisJob.isPending}
                        onClick={() => analysisJob.mutate()}
                        size="xs"
                        variant="secondary"
                      >
                        {analysisJob.isPending ? '创建中…' : '重新分析'}
                      </Button>
                    </div>
                  </div>
                  {analysisJob.error && (
                    <p className="mt-2 text-xs text-destructive">{errorMessage(analysisJob.error)}</p>
                  )}
                  {!knowledge.data?.length ? (
                    latestAnalysis?.status === 'FAILED' ? (
                      <div className="mt-4 rounded border border-destructive/25 bg-destructive/5 p-4 text-xs">
                        <div className="font-medium text-destructive">最近一次 Hermes 分析失败</div>
                        <div className="mt-1 text-muted-foreground">
                          {latestAnalysis.error_code ?? 'ANALYSIS_FAILED'}：
                          {latestAnalysis.error_message ?? '请在任务中心查看详情后重试。'}
                        </div>
                      </div>
                    ) : (
                      <div className="mt-4 rounded bg-(--ui-bg-quaternary) p-4 text-center text-xs text-muted-foreground">
                        {latestAnalysis && ['PENDING', 'RUNNING', 'RETRY_WAIT'].includes(latestAnalysis.status)
                          ? 'Hermes 正在生成知识结果…'
                          : '尚无知识结果'}
                      </div>
                    )
                  ) : knowledgeView === 'categories' ? (
                    knowledge.data.map(document => (
                      <article
                        className="mt-3 rounded-md border border-(--ui-stroke-secondary) bg-background/40 p-4"
                        key={document.id}
                      >
                        <div className="flex items-center justify-between">
                          <strong className="text-xs">{document.document_type.replaceAll('_', ' ')}</strong>
                          <span className="text-[0.6875rem] text-muted-foreground">
                            v{document.version} · {document.model}
                          </span>
                        </div>
                        <pre className="mt-2 whitespace-pre-wrap font-sans text-xs leading-5 text-(--ui-text-secondary)">
                          {readableContent(document.content)}
                        </pre>
                      </article>
                    ))
                  ) : (
                    <div className="mt-4">
                      {knowledgeTimeline.summary && (
                        <article className="rounded-lg border border-primary/20 bg-primary/5 p-4">
                          <div className="flex items-center gap-2 text-xs font-semibold text-primary">
                            <Codicon name="sparkle" />
                            内容概览
                          </div>
                          <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-(--ui-text-secondary)">
                            {knowledgeTimeline.summary}
                          </p>
                        </article>
                      )}

                      {knowledgeTimeline.items.length ? (
                        <div className="mt-4">
                          {knowledgeTimeline.items.map((item, index) => {
                            const active = currentMs >= item.startMs && currentMs <= item.endMs

                            return (
                              <div
                                className="grid grid-cols-[4.5rem_1rem_minmax(0,1fr)] gap-2"
                                key={item.id}
                              >
                                <button
                                  aria-label={`跳转到 ${timestamp(item.startMs)}`}
                                  className="mt-3 flex h-7 items-center justify-center gap-1 rounded font-mono text-[0.6875rem] text-primary hover:bg-primary/10"
                                  onClick={() => seek(item.startMs)}
                                  type="button"
                                >
                                  <Codicon name="play-circle" />
                                  {timestamp(item.startMs)}
                                </button>
                                <div className="flex flex-col items-center">
                                  <span className={`mt-[1.15rem] h-2.5 w-2.5 shrink-0 rounded-full border-2 ${active ? 'border-primary bg-primary' : 'border-primary/60 bg-(--ui-bg-secondary)'}`} />
                                  {index < knowledgeTimeline.items.length - 1 && (
                                    <span className="w-px flex-1 bg-primary/25" />
                                  )}
                                </div>
                                <article className={`mb-3 rounded-lg border p-3.5 ${active ? 'border-primary/40 bg-primary/5' : 'border-(--ui-stroke-secondary) bg-background/40'}`}>
                                  <div className="flex flex-wrap items-center gap-2">
                                    <Badge variant="outline">{item.label}</Badge>
                                    <h4 className="min-w-0 flex-1 text-sm font-semibold">{item.title}</h4>
                                    {item.endMs > item.startMs && (
                                      <span className="text-[0.6875rem] text-muted-foreground">
                                        至 {timestamp(item.endMs)}
                                      </span>
                                    )}
                                  </div>
                                  <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-(--ui-text-secondary)">
                                    {item.body}
                                  </p>
                                </article>
                              </div>
                            )
                          })}
                        </div>
                      ) : (
                        <div className="mt-3 rounded bg-(--ui-bg-quaternary) p-4 text-center text-xs text-muted-foreground">
                          当前结果没有可定位的时间引用，可切换到分类视图查看原始内容。
                        </div>
                      )}
                    </div>
                  )}
                </section>
              </div>

              <aside className="flex min-h-[32rem] flex-col rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary)">
                <div className="border-b border-(--ui-stroke-secondary) p-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-[0.6875rem] font-semibold uppercase tracking-wider text-primary">
                        Transcript
                      </div>
                      <h3 className="mt-1 text-sm font-semibold">字幕时间轴</h3>
                    </div>
                    {transcript.data && (
                      <Badge variant="outline">
                        {transcript.data.language} · v{transcript.data.version}
                      </Badge>
                    )}
                  </div>
                  <Input
                    className="mt-3"
                    onChange={event => setQuery(event.target.value)}
                    placeholder="搜索字幕内容"
                    type="search"
                    value={query}
                  />
                </div>
                <ScrollArea className="h-[34rem] min-h-0 flex-1">
                  {transcript.isLoading ? (
                    <Loader className="mx-auto mt-12" />
                  ) : transcript.isError ? (
                    <div className="p-5 text-center">
                      <p className="text-xs text-muted-foreground">字幕加载失败，可能是服务正在启动或尚未生成 Transcript。</p>
                      <p className="mt-2 break-words text-xs text-destructive">
                        {errorMessage(transcript.error)}
                      </p>
                      <div className="mt-3 flex justify-center gap-2">
                        <Button
                          disabled={transcript.isFetching}
                          onClick={() => void transcript.refetch()}
                          size="xs"
                          variant="secondary"
                        >
                          {transcript.isFetching ? '正在加载…' : '重新加载'}
                        </Button>
                        <Button
                          disabled={transcriptJob.isPending}
                          onClick={() => transcriptJob.mutate()}
                          size="xs"
                        >
                          {transcriptJob.isPending ? '正在创建…' : '生成 Transcript'}
                        </Button>
                      </div>
                      {transcriptJob.error && (
                        <p className="mt-2 text-xs text-destructive">{errorMessage(transcriptJob.error)}</p>
                      )}
                    </div>
                  ) : !visibleSegments.length ? (
                    <div className="p-5 text-center text-xs text-muted-foreground">没有匹配片段</div>
                  ) : (
                    visibleSegments.map(segment => (
                      <button
                        className={`grid w-full grid-cols-[3.5rem_1fr] gap-3 border-b border-(--ui-stroke-secondary) px-3 py-2.5 text-left text-xs hover:bg-(--chrome-action-hover) ${currentMs >= segment.start_ms && currentMs < segment.end_ms ? 'bg-primary/10' : ''}`}
                        key={segment.id}
                        onClick={() => seek(segment.start_ms)}
                        type="button"
                      >
                        <time className="font-mono text-primary">{timestamp(segment.start_ms)}</time>
                        <span className="leading-5 text-(--ui-text-secondary)">{segment.text}</span>
                      </button>
                    ))
                  )}
                </ScrollArea>
              </aside>
            </div>
          </div>
        </ScrollArea>
      )}
    </div>
  )
}
