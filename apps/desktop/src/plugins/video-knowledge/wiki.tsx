import {
  Badge,
  Button,
  Codicon,
  EmptyState,
  Input,
  Loader,
  ScrollArea,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useState } from 'react'

import {
  applyWikiReview,
  askWiki,
  cancelWikiBackfill,
  fetchMedia,
  fetchWikiCatalog,
  fetchWikiDiff,
  fetchWikiHistory,
  fetchWikiIngestions,
  fetchWikiPage,
  fetchWikiSettings,
  fetchWikiSource,
  jobAction,
  lintWikiSemantics,
  lintWikiStructure,
  previewWikiBackfill,
  previewWikiSchema,
  rebuildWikiSearch,
  recompileWikiFusion,
  repairWikiIndex,
  repairWikiLinks,
  resolveWikiCitation,
  rollbackWikiPage,
  saveWikiAnswer,
  searchWiki,
  submitWikiBackfill,
  submitWikiFusionBackfill,
  submitWikiMedia,
  updateWikiSettings,
  withdrawWikiSource
} from './api'
import { errorMessage, timestamp } from './format'
import type { WikiBackfillPreview, WikiIngestion } from './types'
import { WikiMarkdown } from './wiki-markdown'

const STATUS: Record<string, string> = {
  CANCELLED: '已取消',
  CONFLICT: '冲突',
  FAILED: '失败',
  NEW: '新增',
  NO_ANALYSIS: '无分析',
  PENDING: '待入库',
  RUNNING: '处理中',
  SUCCEEDED: '已完成',
  REVIEW_REQUIRED: '待复核',
  PROCESSING: '处理中',
  REVIEW: '待复核',
  SYNCED: '已同步',
  VERSION_UPDATE: '版本更新'
}

function statusText(status?: string) {
  return status ? (STATUS[status] ?? status) : '未入库'
}

function fusionStatusText(status: string) {
  const labels: Record<string, string> = {
    PENDING: '待融合', RUNNING: '融合中', SUCCEEDED: '已完成',
    FAILED: '失败', CANCELLED: '已取消', REVIEW_REQUIRED: '待复核'
  }

  return labels[status] ?? status
}

function latestForMedia(items: WikiIngestion[] | undefined, mediaId: string) {
  return items?.find(item => item.media_id === mediaId)
}

export function WikiMediaStatus({ mediaId, onOpenWiki }: { mediaId: string; onOpenWiki: (pageId: string) => void }) {
  const queryClient = useQueryClient()

  const ingestions = useQuery({
    queryFn: () => fetchWikiIngestions(mediaId),
    queryKey: ['video-knowledge', 'wiki', 'ingestions', mediaId],
    refetchInterval: 5_000
  })

  const current = ingestions.data?.[0]

  const catalog = useQuery({
    queryFn: () => fetchWikiCatalog(),
    queryKey: ['video-knowledge', 'wiki', 'catalog', '', ''],
    refetchInterval: 5_000
  })

  const hasPage = catalog.data?.items.some(item => item.page_id === `video_${mediaId}`) ?? false

  const sync = useMutation({
    mutationFn: () => submitWikiMedia(mediaId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
  })

  const retry = useMutation({
    mutationFn: () => jobAction(current!.job_id, 'retry'),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
  })

  const fuse = useMutation({
    mutationFn: () => submitWikiFusionBackfill([mediaId]),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
  })

  return (
    <section className="rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-3 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Codicon name="book" />
          知识库 <Badge variant="outline">{statusText(current?.wiki_status ?? (hasPage ? 'SYNCED' : undefined))}</Badge>
          {current?.source_revision && <Badge variant="outline">来源已入库</Badge>}
          {current?.source_revision && <Badge variant="outline">知识融合：{fusionStatusText(current.fusion_status)}</Badge>}
        </div>
        <div className="flex flex-wrap gap-2">
          {hasPage && (
            <Button onClick={() => onOpenWiki(`video_${mediaId}`)} size="xs" variant="secondary">
              打开知识页
            </Button>
          )}
          {current?.wiki_status === 'FAILED' || current?.wiki_status === 'CONFLICT' ? (
            <Button disabled={retry.isPending} onClick={() => retry.mutate()} size="xs" variant="secondary">
              重试 Wiki
            </Button>
          ) : !current || current.wiki_status === 'CANCELLED' ? (
            <Button disabled={sync.isPending} onClick={() => sync.mutate()} size="xs" variant="secondary">
              手动入库
            </Button>
          ) : null}
          {current?.source_revision && ['FAILED', 'CANCELLED', 'PENDING'].includes(current.fusion_status) && (
            <Button disabled={fuse.isPending} onClick={() => fuse.mutate()} size="xs" variant="secondary">
              {current.fusion_status === 'PENDING' ? '融合知识' : '重试融合'}
            </Button>
          )}
        </div>
      </div>
      {(sync.error || retry.error || fuse.error) && (
        <p className="mt-2 text-destructive">{errorMessage(sync.error ?? retry.error ?? fuse.error)}</p>
      )}
      {current?.error_code && <p className="mt-2 text-destructive">错误码：{current.error_code}</p>}
    </section>
  )
}

export function WikiView({
  initialPageId,
  onOpenMedia
}: {
  initialPageId?: null | string
  onOpenMedia: (mediaId: string, startMs: number) => void
}) {
  const queryClient = useQueryClient()
  const [pageId, setPageId] = useState<null | string>(initialPageId ?? null)
  const [query, setQuery] = useState('')
  const [question, setQuestion] = useState('')
  const [pageType, setPageType] = useState('')
  const [tag, setTag] = useState('')
  const [sourceRevision, setSourceRevision] = useState<null | string>(null)
  const [preview, setPreview] = useState<null | WikiBackfillPreview[]>(null)
  const [batchId, setBatchId] = useState<null | string>(null)
  const [notice, setNotice] = useState<null | string>(null)
  const [withdrawReason, setWithdrawReason] = useState('')
  const [compareRevision, setCompareRevision] = useState<null | number>(null)
  const [reviewBody, setReviewBody] = useState<null | string>(null)
  const [reviewRevision, setReviewRevision] = useState<null | number>(null)
  const [reviewRunId, setReviewRunId] = useState<null | string>(null)

  const catalog = useQuery({
    queryFn: () => fetchWikiCatalog(pageType || undefined, tag || undefined),
    queryKey: ['video-knowledge', 'wiki', 'catalog', pageType, tag],
    refetchInterval: 10_000
  })

  const media = useQuery({ queryFn: fetchMedia, queryKey: ['video-knowledge', 'media'] })

  const results = useQuery({
    enabled: Boolean(query.trim()),
    queryFn: () => searchWiki(query.trim(), pageType || undefined, tag || undefined),
    queryKey: ['video-knowledge', 'wiki', 'search', query, pageType, tag]
  })

  const visible = query.trim() ? results.data?.items : catalog.data?.items
  const activePageId = query.trim() && visible?.length === 0 ? null : (pageId ?? visible?.[0]?.page_id ?? null)

  const page = useQuery({
    enabled: Boolean(activePageId),
    queryFn: () => fetchWikiPage(activePageId!),
    queryKey: ['video-knowledge', 'wiki', 'page', activePageId]
  })

  const history = useQuery({
    enabled: Boolean(activePageId),
    queryFn: () => fetchWikiHistory(activePageId!),
    queryKey: ['video-knowledge', 'wiki', 'history', activePageId]
  })

  const externalDiff = useQuery({
    enabled: Boolean(activePageId),
    queryFn: () => fetchWikiDiff(activePageId!),
    queryKey: ['video-knowledge', 'wiki', 'diff', activePageId]
  })

  const revisionDiff = useQuery({
    enabled: Boolean(activePageId && compareRevision),
    queryFn: () => fetchWikiDiff(activePageId!, compareRevision!),
    queryKey: ['video-knowledge', 'wiki', 'revision-diff', activePageId, compareRevision]
  })

  const structureAction = useMutation({ mutationFn: lintWikiStructure })
  const semanticAction = useMutation({ mutationFn: () => lintWikiSemantics() })
  const schemaAction = useMutation({ mutationFn: previewWikiSchema })

  const recompileAction = useMutation({
    mutationFn: () => recompileWikiFusion(),
    onSuccess: result => {
      setNotice(`已提交 ${result.job_ids.length} 个规范重编译任务`)
      void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
    }
  })

  const indexAction = useMutation({
    mutationFn: repairWikiIndex,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
  })

  const rollbackAction = useMutation({
    mutationFn: (revision: number) => rollbackWikiPage(activePageId!, revision),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
  })

  const linkAction = useMutation({
    mutationFn: () => repairWikiLinks(activePageId!),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
  })

  const withdrawAction = useMutation({
    mutationFn: (revision: string) => withdrawWikiSource(mediaId!, revision, withdrawReason.trim()),
    onSuccess: () => {
      setSourceRevision(null)
      setWithdrawReason('')
      void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
    }
  })

  const reviewAction = useMutation({
    mutationFn: () => applyWikiReview(activePageId!, reviewRevision!, reviewBody!, reviewRunId!),
    onSuccess: () => {
      setReviewBody(null)
      setReviewRevision(null)
      setReviewRunId(null)
      void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
    }
  })

  const mediaId = page.data?.type === 'video' ? page.data.page_id.replace(/^video_/, '') : null

  const source = useQuery({
    enabled: Boolean(mediaId && sourceRevision),
    queryFn: () => fetchWikiSource(mediaId!, sourceRevision!),
    queryKey: ['video-knowledge', 'wiki', 'source', mediaId, sourceRevision]
  })

  const settings = useQuery({ queryFn: fetchWikiSettings, queryKey: ['video-knowledge', 'wiki', 'settings'] })

  const ingestions = useQuery({
    queryFn: () => fetchWikiIngestions(),
    queryKey: ['video-knowledge', 'wiki', 'ingestions'],
    refetchInterval: 5_000
  })

  const settingChange = useMutation({
    mutationFn: updateWikiSettings,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
  })

  const previewAction = useMutation({ mutationFn: previewWikiBackfill, onSuccess: setPreview })
  const askAction = useMutation({ mutationFn: askWiki })

  const saveAnswerAction = useMutation({
    mutationFn: saveWikiAnswer,
    onSuccess: result => {
      setPageId(result.page_id)
      setNotice(result.unchanged ? '研究答案已在知识库中。' : '研究答案已保存到知识库。')
      void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
    }
  })

  const backfillAction = useMutation({
    mutationFn: submitWikiBackfill,
    onSuccess: result => {
      setBatchId(result.batch_id)
      setNotice(`已提交 ${result.job_ids.length} 个 Wiki 任务`)
      void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
    }
  })

  const fusionBackfill = useMutation({
    mutationFn: () => submitWikiFusionBackfill(),
    onSuccess: result => {
      setNotice(`已提交或保留 ${result.job_ids.length} 个知识融合任务`)
      void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
    }
  })

  const cancelAction = useMutation({
    mutationFn: () => cancelWikiBackfill(batchId!),
    onSuccess: result => {
      setNotice(`已请求取消 ${result.cancelled_job_ids.length} 个任务；已完成页面保留。`)
      void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
    }
  })

  const rebuildAction = useMutation({
    mutationFn: rebuildWikiSearch,
    onSuccess: result => {
      setNotice(`搜索索引已重建：${result.count} 个页面`)
      void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki', 'search'] })
    }
  })

  const citationAction = useMutation({
    mutationFn: (itemKey: string) => resolveWikiCitation(activePageId!, itemKey),
    onSuccess: target => {
      if (target.media_missing) {
        setNotice('媒体已移除；来源快照仍可在此页查看。')
      } else {
        onOpenMedia(target.media_id, target.start_ms)
      }
    },
    onError: error => setNotice(`引用无法回看：${errorMessage(error)}`)
  })

  const currentIngestion = mediaId ? latestForMedia(ingestions.data, mediaId) : null

  const retryAction = useMutation({
    mutationFn: () => jobAction(currentIngestion!.job_id, 'retry'),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
  })

  const syncAction = useMutation({
    mutationFn: () => submitWikiMedia(mediaId!),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
  })

  const actionError =
    askAction.error ??
    saveAnswerAction.error ??
    settingChange.error ??
    previewAction.error ??
    backfillAction.error ??
    fusionBackfill.error ??
    cancelAction.error ??
    rebuildAction.error ??
    syncAction.error ??
    retryAction.error
    ?? structureAction.error ?? semanticAction.error ?? schemaAction.error ?? recompileAction.error
    ?? indexAction.error ?? rollbackAction.error ?? withdrawAction.error ?? linkAction.error ?? reviewAction.error

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <section className="border-b border-(--ui-stroke-secondary) px-5 py-3 text-xs">
        <div className="flex gap-2">
          <Input
            aria-label="向知识库提问"
            maxLength={500}
            onChange={event => setQuestion(event.target.value)}
            placeholder="向知识库提问，回答会核对原视频证据…"
            value={question}
          />
          <Button
            disabled={!question.trim() || !catalog.data?.initialized || askAction.isPending}
            onClick={() => askAction.mutate(question.trim())}
            size="xs"
          >
            {askAction.isPending ? '研究中…' : '提问'}
          </Button>
        </div>
        {askAction.data && !askAction.isPending && (
          <div className="mt-3 space-y-2 rounded border border-(--ui-stroke-secondary) p-3">
            <p className="font-medium">{askAction.data.question}</p>
            <p className="whitespace-pre-wrap">{askAction.data.answer}</p>
            {askAction.data.insufficient_evidence && <Badge variant="outline">证据不足</Badge>}
            {askAction.data.citations.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {askAction.data.citations.map((citation, index) => (
                  <Button
                    key={`${citation.source_revision}-${index}`}
                    onClick={() => onOpenMedia(citation.media_id, citation.start_ms)}
                    size="xs"
                    variant="secondary"
                  >
                    证据 {index + 1} · {timestamp(citation.start_ms)} · 页面修订 {citation.page_revision}
                  </Button>
                ))}
              </div>
            )}
            {!askAction.data.insufficient_evidence && (
              <Button
                disabled={saveAnswerAction.isPending}
                onClick={() => saveAnswerAction.mutate(askAction.data!.run_id)}
                size="xs"
                variant="secondary"
              >
                {saveAnswerAction.isPending ? '保存中…' : '保存到知识库'}
              </Button>
            )}
          </div>
        )}
      </section>
      <div className="flex flex-wrap items-center gap-2 border-b border-(--ui-stroke-secondary) px-5 py-2 text-xs">
        <span className="font-semibold">知识库</span>
        <Badge variant="outline">自动入库：{settings.data?.auto_ingest ? '已开启' : '关闭'}</Badge>
        <Button
          disabled={settingChange.isPending}
          onClick={() => settingChange.mutate(!settings.data?.auto_ingest)}
          size="xs"
          variant="secondary"
        >
          {settings.data?.auto_ingest ? '关闭自动入库' : '开启自动入库'}
        </Button>
        <span className="text-muted-foreground">关闭后不创建新自动任务；已排队任务继续执行。</span>
        <Button
          disabled={rebuildAction.isPending || !catalog.data?.initialized}
          onClick={() => rebuildAction.mutate()}
          size="xs"
          variant="ghost"
        >
          重建搜索索引
        </Button>
      </div>
      <div className="flex flex-wrap items-center gap-2 border-b border-(--ui-stroke-secondary) px-5 py-2 text-xs">
        <Button disabled={previewAction.isPending} onClick={() => previewAction.mutate()} size="xs" variant="secondary">
          预览历史补录
        </Button>
        <Button disabled={fusionBackfill.isPending} onClick={() => fusionBackfill.mutate()} size="xs" variant="secondary">
          补做知识融合
        </Button>
        {preview && (
          <>
            <span>
              新增 {preview.filter(item => item.status === 'NEW').length} · 更新{' '}
              {preview.filter(item => item.status === 'VERSION_UPDATE').length} · 待复核{' '}
              {preview.filter(item => item.status === 'REVIEW').length} · 已同步{' '}
              {preview.filter(item => item.status === 'SYNCED').length} · 无分析{' '}
              {preview.filter(item => item.status === 'NO_ANALYSIS').length}
            </span>
            <Button
              disabled={backfillAction.isPending || !preview.some(item => item.can_submit)}
              onClick={() => backfillAction.mutate()}
              size="xs"
            >
              确认提交 {preview.filter(item => item.can_submit).length} 项
            </Button>
          </>
        )}
        {batchId && (
          <Button disabled={cancelAction.isPending} onClick={() => cancelAction.mutate()} size="xs" variant="ghost">
            取消本批待处理项
          </Button>
        )}
      </div>
      <section className="border-b border-(--ui-stroke-secondary) px-5 py-2 text-xs">
        <div className="flex flex-wrap gap-2">
          <Button disabled={!catalog.data?.initialized || structureAction.isPending} onClick={() => structureAction.mutate()} size="xs" variant="secondary">结构巡检</Button>
          <Button disabled={!catalog.data?.initialized || semanticAction.isPending} onClick={() => semanticAction.mutate()} size="xs" variant="secondary">{semanticAction.isPending ? '语义巡检中…' : '语义巡检'}</Button>
          <Button disabled={!catalog.data?.initialized || schemaAction.isPending} onClick={() => schemaAction.mutate()} size="xs" variant="ghost">规范影响预览</Button>
          <Button disabled={!catalog.data?.initialized || indexAction.isPending} onClick={() => indexAction.mutate()} size="xs" variant="ghost">修复目录索引</Button>
        </div>
        {structureAction.data && <div className="mt-2 space-y-1">
          <p>结构问题 {structureAction.data.issues.length} 项 · Wiki 修订 {structureAction.data.wiki_revision}</p>
          {structureAction.data.issues.map((item, index) => <p key={`${item.code}-${index}`}>{item.code} · {item.page_id ?? '目录'} · {item.detail}</p>)}
        </div>}
        {semanticAction.data && <div className="mt-2 space-y-1">
          <p>语义建议 {semanticAction.data.issues.length} 项 · 报告 {semanticAction.data.run_id}</p>
          {semanticAction.data.issues.map((item, index) => <p key={`${item.code}-${index}`}>{item.code} · {item.description} · {item.citations?.map(ref => `${ref.page_id}@${ref.page_revision}`).join(', ')}</p>)}
        </div>}
        {schemaAction.data && <div className="mt-2 flex flex-wrap items-center gap-2">
          <span>规范版本 {schemaAction.data.schema_version} · 可能影响 {schemaAction.data.count} 页 · 版本待更新 {schemaAction.data.outdated_page_ids.length} 页</span>
          <Button disabled={recompileAction.isPending} onClick={() => recompileAction.mutate()} size="xs" variant="secondary">按当前规范重新融合</Button>
        </div>}
      </section>
      {preview && (
        <details className="border-b border-(--ui-stroke-secondary) px-5 py-2 text-xs">
          <summary className="cursor-pointer">查看 {preview.length} 项补录预览</summary>
          <div className="mt-2 max-h-40 space-y-1 overflow-y-auto">
            {preview.map(item => (
              <div className="flex items-center justify-between gap-2" key={item.media_id}>
                <span className="truncate">
                  {media.data?.find(row => row.id === item.media_id)?.title ?? item.media_id}
                </span>
                <span className="shrink-0 text-muted-foreground">{statusText(item.status)}</span>
              </div>
            ))}
          </div>
        </details>
      )}
      {(notice || actionError) && (
        <p className={`px-5 py-2 text-xs ${actionError ? 'text-destructive' : 'text-muted-foreground'}`}>
          {actionError ? errorMessage(actionError) : notice}
        </p>
      )}
      <div className="flex min-h-0 flex-1">
        <aside className="flex w-72 shrink-0 flex-col border-r border-(--ui-stroke-secondary)">
          <div className="space-y-2 border-b border-(--ui-stroke-secondary) p-3">
            <Input
              aria-label="搜索知识库"
              onChange={event => setQuery(event.target.value)}
              placeholder="搜索标题或正文…"
              type="search"
              value={query}
            />
            <div className="flex gap-2">
              <select
                aria-label="页面类型"
                className="min-w-0 flex-1 rounded border border-(--ui-stroke-secondary) bg-background p-1 text-xs"
                onChange={event => setPageType(event.target.value)}
                value={pageType}
              >
                <option value="">所有类型</option>
                {catalog.data?.types.map(value => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
              <select
                aria-label="标签"
                className="min-w-0 flex-1 rounded border border-(--ui-stroke-secondary) bg-background p-1 text-xs"
                onChange={event => setTag(event.target.value)}
                value={tag}
              >
                <option value="">所有标签</option>
                {catalog.data?.tags.map(value => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <ScrollArea className="min-h-0 flex-1">
            {catalog.isLoading ? (
              <div className="p-4">
                <Loader />
              </div>
            ) : catalog.error ? (
              <EmptyState description={errorMessage(catalog.error)} title="目录无法读取" />
            ) : results.error && query.trim() ? (
              <EmptyState description={errorMessage(results.error)} title="搜索失败" />
            ) : !catalog.data?.initialized ? (
              <EmptyState description="开启自动入库，或从媒体详情手动入库后即可阅读。" title="知识库尚未初始化" />
            ) : visible?.length ? (
              visible.map(item => (
                <button
                  className={`w-full border-b border-(--ui-stroke-secondary) p-3 text-left hover:bg-(--chrome-action-hover) ${activePageId === item.page_id ? 'bg-(--ui-bg-quaternary)' : ''}`}
                  key={item.page_id}
                  onClick={() => {
                    setPageId(item.page_id)
                    setCompareRevision(null)
                    setReviewBody(null)
                    setReviewRevision(null)
                    setReviewRunId(null)
                    setSourceRevision(null)
                    setNotice(null)
                  }}
                  type="button"
                >
                  <div className="wrap-anywhere text-xs leading-5 font-medium whitespace-normal">{item.title}</div>
                  <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[0.68rem] text-muted-foreground">
                    <span>{item.type}</span>
                    <span>修订 {item.revision}</span>
                    {item.tags.map(value => (
                      <span className="wrap-anywhere" key={value}>{value}</span>
                    ))}
                  </div>
                  {'excerpt' in item && typeof item.excerpt === 'string' && (
                    <p className="mt-1 line-clamp-2 text-[0.68rem] text-muted-foreground">{item.excerpt}</p>
                  )}
                </button>
              ))
            ) : (
              <EmptyState
                description={query.trim() ? '换个关键词或筛选条件试试。' : '目前没有已发布的页面。'}
                title="没有结果"
              />
            )}
          </ScrollArea>
        </aside>
        <main className="flex min-w-0 flex-1 flex-col">
          {page.isLoading ? (
            <div className="p-5">
              <Loader />
            </div>
          ) : page.error ? (
            <EmptyState description={errorMessage(page.error)} title="页面无法读取" />
          ) : !page.data ? (
            <EmptyState description="从左侧目录选择页面。" title="选择知识页" />
          ) : (
            <ScrollArea className="min-h-0 flex-1">
              <div className="mx-auto max-w-4xl space-y-5 p-5">
                <header className="border-b border-(--ui-stroke-secondary) pb-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-xl font-semibold">{page.data.title}</h2>
                    <Badge variant="outline">{page.data.type}</Badge>
                    <Badge variant="outline">修订 {page.data.revision}</Badge>
                    {currentIngestion && <Badge variant="outline">{statusText(currentIngestion.wiki_status)}</Badge>}
                    {currentIngestion?.source_revision && <Badge variant="outline">知识融合：{fusionStatusText(currentIngestion.fusion_status)}</Badge>}
                  </div>
                  {page.data.tags.length > 0 && (
                    <p className="mt-2 text-xs text-muted-foreground">标签：{page.data.tags.join(' · ')}</p>
                  )}
                  {mediaId && (
                    <div className="mt-3 flex gap-2">
                      <Button
                        disabled={syncAction.isPending}
                        onClick={() => syncAction.mutate()}
                        size="xs"
                        variant="secondary"
                      >
                        手动同步
                      </Button>
                      {currentIngestion &&
                        ['FAILED', 'CONFLICT', 'CANCELLED'].includes(currentIngestion.wiki_status) && (
                          <Button
                            disabled={retryAction.isPending}
                            onClick={() => retryAction.mutate()}
                            size="xs"
                            variant="secondary"
                          >
                            重试 Wiki
                          </Button>
                        )}
                    </div>
                  )}
                  {currentIngestion?.error_code && (
                    <p className="mt-2 text-xs text-destructive">任务失败：{currentIngestion.error_code}</p>
                  )}
                  {externalDiff.data?.changed && <div className="mt-3 rounded border border-destructive p-2 text-xs">
                    <p className="font-semibold">检测到页面外部编辑，同步会报告冲突。</p>
                    <pre className="max-h-40 overflow-auto whitespace-pre-wrap">{externalDiff.data.diff}</pre>
                  </div>}
                  {history.data && history.data.length > 1 && <details className="mt-3 text-xs">
                    <summary className="cursor-pointer">修订历史与回退</summary>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {history.data.filter(item => item.revision < page.data!.revision).map(item => <div className="flex gap-1" key={item.commit_id}>
                        <Button onClick={() => setCompareRevision(item.revision)} size="xs" variant="ghost">查看修订 {item.revision} 差异</Button>
                        <Button
                          disabled={rollbackAction.isPending || externalDiff.data?.changed || compareRevision !== item.revision}
                          onClick={() => rollbackAction.mutate(item.revision)}
                          size="xs" variant="secondary"
                        >回退</Button>
                      </div>)}
                    </div>
                    {revisionDiff.data && <pre className="mt-2 max-h-60 overflow-auto whitespace-pre-wrap">{revisionDiff.data.diff}</pre>}
                  </details>}
                  <Button disabled={linkAction.isPending || externalDiff.data?.changed} onClick={() => linkAction.mutate()} size="xs" variant="ghost">修复此页断链</Button>
                </header>
                <WikiMarkdown
                  onCitation={itemKey => citationAction.mutate(itemKey)}
                  onPage={target => {
                    setPageId(target)
                    setCompareRevision(null)
                    setReviewBody(null)
                    setReviewRevision(null)
                    setReviewRunId(null)
                    setSourceRevision(null)
                  }}
                  page={page.data}
                />
                {semanticAction.data?.issues.some(issue => issue.citations?.some(ref => ref.page_id === page.data!.page_id && ref.page_revision === page.data!.revision)) &&
                  <section className="rounded border border-(--ui-stroke-secondary) p-3 text-xs">
                    <Button onClick={() => {
                      setReviewBody(page.data!.body)
                      setReviewRevision(page.data!.revision)
                      setReviewRunId(semanticAction.data!.run_id)
                    }} size="xs" variant="secondary">人工复核此页建议</Button>
                    {reviewBody !== null && <div className="mt-2 space-y-2">
                      <textarea aria-label="复核后的页面正文" className="min-h-60 w-full rounded border border-(--ui-stroke-secondary) bg-background p-2" maxLength={100000} onChange={event => setReviewBody(event.target.value)} value={reviewBody} />
                      <div className="flex gap-2">
                        <Button disabled={!reviewBody.trim() || reviewAction.isPending || page.data!.revision !== reviewRevision} onClick={() => reviewAction.mutate()} size="xs">提交复核修订</Button>
                        <Button onClick={() => { setReviewBody(null); setReviewRevision(null); setReviewRunId(null) }} size="xs" variant="ghost">取消</Button>
                      </div>
                    </div>}
                  </section>}
                <section className="border-t border-(--ui-stroke-secondary) pt-4 text-xs">
                  <h3 className="font-semibold">来源修订</h3>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {page.data.source_refs.map(revision => (
                      <Button key={revision} onClick={() => setSourceRevision(revision)} size="xs" variant="ghost">
                        {revision.slice(0, 18)}…
                      </Button>
                    ))}
                  </div>
                </section>
                {sourceRevision && (
                  <section className="rounded border border-(--ui-stroke-secondary) p-3 text-xs">
                    <h3 className="font-semibold">来源快照 · {sourceRevision.slice(0, 18)}…</h3>
                    {!mediaId ? (
                      <p className="mt-2 text-muted-foreground">请从视频页查看分段来源。</p>
                    ) : source.isLoading ? (
                      <Loader />
                    ) : source.error ? (
                      <p className="text-destructive">来源失效：{errorMessage(source.error)}</p>
                    ) : source.data ? (
                      <div className="mt-2 max-h-60 space-y-2 overflow-y-auto">
                        {source.data.transcript.segments.map(segment => (
                          <p key={segment.id}>
                            <span className="mr-2 font-mono text-primary">{timestamp(segment.start_ms)}</span>
                            {segment.text}
                          </p>
                        ))}
                      </div>
                    ) : null}
                  </section>
                )}
                {mediaId && sourceRevision && <div className="flex gap-2 text-xs">
                  <Input aria-label="撤回来源原因" maxLength={500} onChange={event => setWithdrawReason(event.target.value)} placeholder="撤回来源原因" value={withdrawReason} />
                  <Button disabled={!withdrawReason.trim() || withdrawAction.isPending} onClick={() => withdrawAction.mutate(sourceRevision)} size="xs" variant="secondary">撤回此来源</Button>
                </div>}
                {page.data.citation_refs.length > 0 && (
                  <section className="border-t border-(--ui-stroke-secondary) pt-4 text-xs">
                    <h3 className="font-semibold">时间引用</h3>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {page.data.citation_refs.map(ref => (
                        <Button
                          key={`${ref.source_revision}-${ref.item_key}`}
                          onClick={() => citationAction.mutate(ref.item_key)}
                          size="xs"
                          variant="secondary"
                        >
                          {ref.item_key} · {timestamp(ref.start_ms)}
                        </Button>
                      ))}
                    </div>
                  </section>
                )}
                {page.data.backlinks.length > 0 && (
                  <section className="border-t border-(--ui-stroke-secondary) pt-4 text-xs">
                    <h3 className="font-semibold">反向链接</h3>
                    {page.data.backlinks.map(item => (
                      <button
                        className="mt-2 block text-primary underline"
                        key={item.page_id}
                        onClick={() => setPageId(item.page_id)}
                        type="button"
                      >
                        {item.title}
                      </button>
                    ))}
                  </section>
                )}
              </div>
            </ScrollArea>
          )}
        </main>
      </div>
    </div>
  )
}
