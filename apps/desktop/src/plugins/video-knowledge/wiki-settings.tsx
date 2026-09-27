import { Badge, Button, useMutation, useQuery, useQueryClient } from '@hermes/plugin-sdk'
import { type ReactNode, useId, useState } from 'react'

import {
  cancelWikiBackfill, fetchMedia, fetchWikiCatalog, fetchWikiSettings,
  lintWikiSemantics, lintWikiStructure, previewWikiBackfill, previewWikiSchema,
  rebuildWikiSearch, recompileWikiFusion, repairWikiIndex, submitWikiBackfill,
  submitWikiFusionBackfill, updateWikiSettings
} from './api'
import { errorMessage } from './format'
import type { WikiLintIssue, WikiSemanticLint } from './types'

const BACKFILL_STATUS: Record<string, string> = {
  NEW: '新增', VERSION_UPDATE: '版本更新', REVIEW: '待复核', SYNCED: '已同步', NO_ANALYSIS: '无分析'
}

const ISSUE_LABELS: Record<string, string> = {
  ORPHAN_PAGE: '页面缺少关联', BROKEN_LINK: '失效链接', INDEX_MISSING: '目录漏项',
  INDEX_STALE: '目录过期项', INDEX_EXTERNAL_EDIT: '目录被外部修改', MISSING_SOURCE: '来源缺失',
  INVALID_SOURCE_REF: '来源引用无效', WITHDRAWN_SUPPORT: '引用了已撤回来源', INVALID_TAG: '标签不符合规范',
  EXTERNAL_EDIT: '页面被外部修改', EXTERNAL_DELETE: '页面文件被删除',
  UNRESOLVED_DISPUTE: '观点争议待复核', DUPLICATE_ENTITY: '疑似重复实体', STALE_SOURCE_VERSION: '来源版本待核对',
  duplicate_claims_in_page: '页内主张重复', duplicate_entity_pages: '实体页面重复',
  duplicate_content_across_pages: '跨页面内容重复', contradiction_unflagged: '矛盾观点未标注',
  unsupported_claim: '主张缺少支撑'
}

// Mutation data resets on retries; retain the last successful report separately.
function useMaintenanceAction<T, V = void>(run: (value: V) => Promise<T>, onSuccess?: (value: T) => void) {
  const [last, setLast] = useState<{ data: T; at: number } | null>(null)

  const mutation = useMutation({ mutationFn: run, onSuccess: (value: T) => {
    setLast({ data: value, at: Date.now() })
    onSuccess?.(value)
  } })

  return { ...mutation, result: last?.data, resultAt: last?.at }
}

function Feedback({ pending, error, at, children }: {
  pending?: boolean; error?: unknown; at?: number; children?: ReactNode
}) {
  return <div aria-live="polite" className="space-y-2">
    {pending && <p role="status">正在运行，请稍候…</p>}
    {Boolean(error) && <p className="text-destructive" role="alert">本次操作失败：{errorMessage(error)}{at ? '。下方保留上次成功结果。' : ''}</p>}
    {at && <p className="text-muted-foreground">上次成功：{new Date(at).toLocaleString()}</p>}
    {children}
  </div>
}

function MaintenanceCard({ title, description, button, onRun, disabled, pending, error, at, summary, children }: {
  title: string; description: string; button?: string; onRun: () => void; disabled?: boolean
  pending: boolean; error: unknown; at?: number; summary?: string; children?: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const id = useId()

  return <section aria-label={title} className="rounded-md border border-(--ui-stroke-secondary)">
    <div className="flex flex-wrap items-center justify-between gap-3 p-3">
      <div className="min-w-0 flex-1 space-y-1">
        <h5 className="font-medium">{title}</h5>
        <p className="text-muted-foreground">{description}</p>
        <p className={error ? 'text-destructive' : 'text-muted-foreground'}>{pending ? '运行中…' : error ? '本次运行失败' : summary ?? '尚未运行'}</p>
      </div>
      <div className="flex items-center gap-2">
        <Button disabled={disabled || pending} onClick={() => { setOpen(true); onRun() }} size="xs" variant="secondary">{button ?? title}</Button>
        <Button aria-controls={id} aria-expanded={open} aria-label={`${open ? '收起' : '展开'}${title}结果`} onClick={() => setOpen(!open)} size="xs" variant="ghost">{open ? '收起' : '展开'}</Button>
      </div>
    </div>
    <div className="border-t border-(--ui-stroke-secondary) p-3" hidden={!open} id={id}>
      <Feedback at={at} error={error} pending={pending}>{children ?? <p className="text-muted-foreground">运行后在此查看结果。</p>}</Feedback>
    </div>
  </section>
}

export interface WikiSettingsProps {
  onOpenWiki: (pageId: string) => void
  onSemanticReport: (report: WikiSemanticLint) => void
  semanticReport: WikiSemanticLint | null
  onOpenJobs?: () => void
}

export function WikiSettingsSection({ onOpenWiki, onSemanticReport, semanticReport, onOpenJobs }: WikiSettingsProps) {
  const queryClient = useQueryClient()
  const [confirmRecompile, setConfirmRecompile] = useState(false)
  const catalog = useQuery({ queryFn: () => fetchWikiCatalog(), queryKey: ['video-knowledge', 'wiki', 'catalog', '', ''] })
  const settings = useQuery({ queryFn: fetchWikiSettings, queryKey: ['video-knowledge', 'wiki', 'settings'] })

  const invalidateWiki = () => { void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki'] }) }
  const setting = useMaintenanceAction(updateWikiSettings, result => queryClient.setQueryData(['video-knowledge', 'wiki', 'settings'], result))
  const preview = useMaintenanceAction(previewWikiBackfill)
  const media = useQuery({ enabled: Boolean(preview.result), queryFn: fetchMedia, queryKey: ['video-knowledge', 'media'] })
  const backfill = useMaintenanceAction(submitWikiBackfill, invalidateWiki)
  const cancel = useMaintenanceAction(cancelWikiBackfill, invalidateWiki)
  const fusion = useMaintenanceAction(() => submitWikiFusionBackfill(), invalidateWiki)
  const structure = useMaintenanceAction(lintWikiStructure)
  const semantic = useMaintenanceAction(() => lintWikiSemantics(), onSemanticReport)
  const schema = useMaintenanceAction(previewWikiSchema, () => setConfirmRecompile(false))
  const recompile = useMaintenanceAction(() => recompileWikiFusion(), () => { setConfirmRecompile(false); invalidateWiki() })
  const search = useMaintenanceAction(rebuildWikiSearch, () => { void queryClient.invalidateQueries({ queryKey: ['video-knowledge', 'wiki', 'search'] }) })
  const index = useMaintenanceAction(repairWikiIndex, invalidateWiki)
  const report = semantic.result ?? semanticReport
  const titleFor = (id: string) => catalog.data?.items.find(page => page.page_id === id)?.title ?? id
  const jobsLink = onOpenJobs && <Button onClick={onOpenJobs} size="xs" variant="ghost">前往任务中心</Button>
  const groups = new Map<string, WikiLintIssue[]>()

  for (const issue of structure.result?.issues ?? []) {groups.set(issue.code, [...(groups.get(issue.code) ?? []), issue])}
  const ready = Boolean(catalog.data?.initialized)
  const cardState = (action: { isPending: boolean; error: unknown; resultAt?: number }) => ({ pending: action.isPending, error: action.error, at: action.resultAt })
  const cancellationIsCurrent = cancel.variables === backfill.result?.batch_id

  return <section className="overflow-hidden rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary)">
    <header className="px-5 pt-5 pb-2">
      <h3 className="text-sm font-semibold">知识库设置与维护</h3>
      <p className="mt-1 text-xs text-muted-foreground">按用途选择操作，结果分别保留在对应卡片中。离开本页后，临时结果会清除。</p>
    </header>
    <div className="space-y-6 p-5 text-xs">
      {catalog.error && <p className="text-destructive" role="alert">知识库状态加载失败：{errorMessage(catalog.error)}</p>}
      {!catalog.isLoading && !catalog.error && !ready && <p className="text-muted-foreground">知识库尚未初始化；完成入库后可进行巡检和索引维护。</p>}
      <section aria-label="入库与融合" className="space-y-3">
        <h4 className="text-sm font-semibold">入库与融合</h4>
        <section aria-label="自动入库" className="space-y-2 rounded-md border border-(--ui-stroke-secondary) p-3">
          <div className="flex flex-wrap items-center gap-3">
            <h5 className="font-medium">自动入库</h5>
            <Badge variant="outline">{settings.isLoading ? '加载中…' : !settings.data ? '状态不可用' : settings.data.auto_ingest ? '已开启' : '已关闭'}</Badge>
            <Button disabled={!settings.data || setting.isPending} onClick={() => setting.mutate(!settings.data?.auto_ingest)} size="xs" variant="secondary">{settings.data?.auto_ingest ? '关闭自动入库' : '开启自动入库'}</Button>
          </div>
          <p className="text-muted-foreground">关闭后不创建新自动任务；已排队任务继续执行。</p>
          <Feedback {...cardState(setting)} error={setting.error ?? settings.error} />
        </section>
        <MaintenanceCard button="预览历史补录" description="先查看历史内容的入库状态，再提交可补录项。" onRun={() => preview.mutate()} title="历史补录" {...cardState(preview)} summary={preview.result && `预览 ${preview.result.length} 项`}>
          {preview.result && <div className="space-y-3">
            <p>{Object.entries(BACKFILL_STATUS).map(([status, label]) => `${label} ${preview.result!.filter(item => item.status === status).length}`).join(' · ')}</p>
            <details><summary className="cursor-pointer">查看 {preview.result.length} 项补录预览</summary>
              <div className="mt-2 max-h-60 space-y-2 overflow-y-auto">{preview.result.map(item => <div className="flex justify-between gap-3" key={item.media_id}>
                <span className="min-w-0 break-words">{media.data?.find(row => row.id === item.media_id)?.title ?? item.media_id}</span>
                <span className="shrink-0 text-muted-foreground">{BACKFILL_STATUS[item.status] ?? item.status}</span>
              </div>)}</div>
            </details>
            <Button disabled={preview.isPending || Boolean(preview.error) || backfill.isPending || cancel.isPending || !preview.result.some(item => item.can_submit)} onClick={() => backfill.mutate()} size="xs">确认提交 {preview.result.filter(item => item.can_submit).length} 项</Button>
          </div>}
          <section aria-label="补录任务" className="mt-3 space-y-2">
            <Feedback {...cardState(backfill)}>{backfill.result && <p>已提交 {backfill.result.job_ids.length} 个 Wiki 任务。{jobsLink}</p>}</Feedback>
            {backfill.result && <Button disabled={backfill.isPending || cancel.isPending || (cancellationIsCurrent && cancel.isSuccess)} onClick={() => cancel.mutate(backfill.result!.batch_id)} size="xs" variant="ghost">取消本批待处理项</Button>}
            {cancellationIsCurrent && <Feedback {...cardState(cancel)}>{cancel.result && <p>已请求取消 {cancel.result.cancelled_job_ids.length} 个任务；已完成页面保留。</p>}</Feedback>}
          </section>
        </MaintenanceCard>
        <MaintenanceCard description="为已入库内容补做跨来源知识融合，会调用模型。" onRun={() => fusion.mutate()} title="补做知识融合" {...cardState(fusion)} summary={fusion.result && `${fusion.result.job_ids.length} 个融合任务`}>
          {fusion.result && <p>已提交或保留 {fusion.result.job_ids.length} 个知识融合任务。{jobsLink}</p>}
        </MaintenanceCard>
      </section>
      <section aria-label="检查与复核" className="space-y-3">
        <h4 className="text-sm font-semibold">检查与复核</h4>
        <MaintenanceCard description="检查页面关联、链接、来源和目录结构，不修改页面。" disabled={!ready} onRun={() => structure.mutate()} title="结构巡检" {...cardState(structure)} summary={structure.result && `${structure.result.issues.length} 项待检查`}>
          {structure.result && <div className="space-y-3">
            <p>结构问题 {structure.result.issues.length} 项 · Wiki 修订 {structure.result.wiki_revision}</p>
            {!structure.result.issues.length && <p>未发现结构问题。</p>}
            {Array.from(groups, ([code, issues]) => <details key={code}>
              <summary className="cursor-pointer">{ISSUE_LABELS[code] ?? code} · {issues.length} 项</summary>
              {code === 'ORPHAN_PAGE' && <p className="my-2 text-muted-foreground">这些页面缺少其他正文页面的链接，属于关联建议，不代表内容损坏。可打开页面检查是否需要补充关联。</p>}
              <div className="mt-2 max-h-64 space-y-2 overflow-y-auto">{issues.map((issue, i) => <article className="rounded border border-(--ui-stroke-secondary) p-2" key={i}>
                {issue.page_id ? <Button onClick={() => onOpenWiki(issue.page_id!)} size="xs" variant="ghost">{titleFor(issue.page_id)}</Button> : <p>知识库目录</p>}
                <details className="mt-1 break-all text-muted-foreground"><summary className="cursor-pointer">技术详情</summary>{code} · {issue.page_id} · {issue.detail ?? issue.description}</details>
              </article>)}</div>
            </details>)}
          </div>}
        </MaintenanceCard>
        <MaintenanceCard description="由模型检查重复、矛盾和证据支撑，生成建议后人工复核；会消耗 token。" disabled={!ready} onRun={() => semantic.mutate()} title="语义巡检" {...cardState(semantic)} summary={report ? `${report.issues.length} 条复核建议` : undefined}>
          {report && <div className="space-y-3">
            <p>语义建议 {report.issues.length} 项 · Wiki 修订 {report.wiki_revision}</p>
            {!report.issues.length && <p>本次未发现语义问题。</p>}
            <div className="max-h-96 space-y-3 overflow-y-auto">{report.issues.map((issue, i) => <article className="space-y-2 rounded border border-(--ui-stroke-secondary) p-3" key={i}>
              <h6 className="font-medium">{ISSUE_LABELS[issue.code] ?? issue.code}</h6>
              <p className="whitespace-pre-wrap break-words leading-relaxed">{issue.description ?? issue.detail}</p>
              <div className="flex flex-wrap gap-2">{Array.from(new Set(issue.citations?.map(ref => ref.page_id))).map(id => <Button key={id} onClick={() => onOpenWiki(id)} size="xs" variant="ghost">查看并复核 {titleFor(id)}</Button>)}</div>
            </article>)}</div>
            <details className="break-all text-muted-foreground"><summary className="cursor-pointer">报告信息</summary>报告 {report.run_id}</details>
          </div>}
        </MaintenanceCard>
        <MaintenanceCard description="查看规范版本和页面版本差异，预览本身不启动重新融合。" disabled={!ready} onRun={() => schema.mutate()} title="规范影响预览" {...cardState(schema)} summary={schema.result && `${schema.result.outdated_page_ids.length} 项版本待更新`}>
          {schema.result && <div className="space-y-3">
            <p>规范版本 {schema.result.schema_version} · 可能影响 {schema.result.count} 项 · 版本待更新 {schema.result.outdated_page_ids.length} 项</p>
            <p className="text-muted-foreground">版本差异用于筛查，不代表每个页面都需要修改。</p>
            <details><summary className="cursor-pointer">查看版本待更新页面</summary><div className="mt-2 max-h-60 space-y-1 overflow-y-auto">{schema.result.outdated_page_ids.map(id => <div key={id}><Button onClick={() => onOpenWiki(id)} size="xs" variant="ghost">{titleFor(id)}</Button></div>)}</div></details>
            <section aria-label="按当前规范重新融合" className="space-y-2 border-t border-(--ui-stroke-secondary) pt-3">
              <h6 className="font-medium">按当前规范重新融合</h6>
              <p className="text-muted-foreground">将处理全部符合条件的已入库来源，不限于版本待更新页面。会调用模型，可能消耗较多 token，实际任务数以提交结果为准。</p>
              {confirmRecompile ? <div className="flex flex-wrap items-center gap-2">
                <span>确认开始重新融合？</span>
                <Button disabled={recompile.isPending || schema.isPending || Boolean(schema.error)} onClick={() => recompile.mutate()} size="xs">确认重新融合</Button>
                <Button disabled={recompile.isPending} onClick={() => setConfirmRecompile(false)} size="xs" variant="ghost">取消</Button>
              </div> : <Button disabled={recompile.isPending || schema.isPending || Boolean(schema.error)} onClick={() => setConfirmRecompile(true)} size="xs" variant="secondary">按当前规范重新融合</Button>}
              <Feedback {...cardState(recompile)}>{recompile.result && <p>已提交或保留 {recompile.result.job_ids.length} 个规范重编译任务。{jobsLink}</p>}</Feedback>
            </section>
          </div>}
        </MaintenanceCard>
      </section>
      <section aria-label="索引维护" className="space-y-3">
        <h4 className="text-sm font-semibold">索引维护</h4>
        <MaintenanceCard description="根据现有知识页重建搜索数据，适用于搜索缺失或不准确。" disabled={!ready} onRun={() => search.mutate()} title="重建搜索索引" {...cardState(search)} summary={search.result && `已索引 ${search.result.count} 个页面`}>
          {search.result && <p>搜索索引已重建：{search.result.count} 个页面。</p>}
        </MaintenanceCard>
        <MaintenanceCard description="重新生成知识库导航目录，适用于目录漏项或过期。" disabled={!ready} onRun={() => index.mutate()} title="修复目录索引" {...cardState(index)} summary={index.result && `已修复 · Wiki 修订 ${index.result.wiki_revision}`}>
          {index.result && <div className="space-y-2"><p>目录索引已修复 · Wiki 修订 {index.result.wiki_revision}</p><details className="break-all text-muted-foreground"><summary className="cursor-pointer">提交信息</summary>{index.result.commit_id}</details></div>}
        </MaintenanceCard>
      </section>
    </div>
  </section>
}
