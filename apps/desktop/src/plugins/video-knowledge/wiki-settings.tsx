import { Badge, Button, useMutation, useQuery, useQueryClient } from '@hermes/plugin-sdk'
import { useState } from 'react'

import {
  cancelWikiBackfill,
  fetchMedia,
  fetchWikiCatalog,
  fetchWikiSettings,
  lintWikiSemantics,
  lintWikiStructure,
  previewWikiBackfill,
  previewWikiSchema,
  rebuildWikiSearch,
  recompileWikiFusion,
  repairWikiIndex,
  submitWikiBackfill,
  submitWikiFusionBackfill,
  updateWikiSettings
} from './api'
import { errorMessage } from './format'
import type { WikiBackfillPreview, WikiSemanticLint } from './types'

const BACKFILL_STATUS: Record<string, string> = {
  NEW: '新增', VERSION_UPDATE: '版本更新', REVIEW: '待复核', SYNCED: '已同步', NO_ANALYSIS: '无分析'
}

export interface WikiSettingsProps {
  onOpenWiki: (pageId: string) => void
  onSemanticReport: (report: WikiSemanticLint) => void
  semanticReport: WikiSemanticLint | null
}

export function WikiSettingsSection({ onOpenWiki, onSemanticReport, semanticReport }: WikiSettingsProps) {
  const queryClient = useQueryClient()
  const [preview, setPreview] = useState<WikiBackfillPreview[] | null>(null)
  const [batchId, setBatchId] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const catalog = useQuery({ queryFn: () => fetchWikiCatalog(), queryKey: ['video-knowledge', 'wiki', 'catalog', '', ''] })
  const settings = useQuery({ queryFn: fetchWikiSettings, queryKey: ['video-knowledge', 'wiki', 'settings'] })
  const media = useQuery({ enabled: preview !== null, queryFn: fetchMedia, queryKey: ['video-knowledge', 'media'] })

  const structureAction = useMutation({ mutationFn: lintWikiStructure })
  const semanticAction = useMutation({ mutationFn: () => lintWikiSemantics(), onSuccess: onSemanticReport })
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

  const settingChange = useMutation({
    mutationFn: updateWikiSettings,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] })
  })

  const previewAction = useMutation({ mutationFn: previewWikiBackfill, onSuccess: setPreview })

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

  const actionError = settings.error ?? catalog.error ?? settingChange.error ?? previewAction.error
    ?? backfillAction.error ?? fusionBackfill.error ?? cancelAction.error ?? rebuildAction.error
    ?? structureAction.error ?? semanticAction.error ?? schemaAction.error ?? recompileAction.error ?? indexAction.error

  return (
    <section className="overflow-hidden rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary)">
      <header className="px-5 pt-5 pb-2">
        <h3 className="text-sm font-semibold">知识库设置与维护</h3>
        <p className="mt-1 text-xs text-muted-foreground">管理自动入库、历史补录、知识融合和索引巡检。</p>
      </header>
      <div className="flex flex-wrap items-center gap-2 border-b border-(--ui-stroke-secondary) px-5 py-2 text-xs">
        <span className="font-semibold">知识库</span>
        <Badge variant="outline">自动入库：{settings.isLoading ? '加载中…' : settings.data?.auto_ingest ? '已开启' : '关闭'}</Badge>
        <Button
          disabled={!settings.data || settingChange.isPending}
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
        {semanticReport && <div className="mt-2 space-y-1">
          <p>语义建议 {semanticReport.issues.length} 项 · 报告 {semanticReport.run_id}</p>
          {semanticReport.issues.map((item, index) => <p key={`${item.code}-${index}`}>{item.code} · {item.description} · {Array.from(new Set(item.citations?.map(ref => ref.page_id))).map(pageId => <Button key={pageId} onClick={() => onOpenWiki(pageId)} size="xs" variant="ghost">查看并复核 {catalog.data?.items.find(page => page.page_id === pageId)?.title ?? pageId}</Button>)}</p>)}
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
                <span className="shrink-0 text-muted-foreground">{BACKFILL_STATUS[item.status] ?? item.status}</span>
              </div>
            ))}
          </div>
        </details>
      )}
      {(notice || actionError) && <p className={`px-5 py-3 text-xs ${actionError ? 'text-destructive' : 'text-muted-foreground'}`} role="status">{actionError ? errorMessage(actionError) : notice}</p>}
    </section>
  )
}
