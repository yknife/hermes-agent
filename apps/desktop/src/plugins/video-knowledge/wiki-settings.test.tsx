import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import * as api from './api'
import type { WikiPage, WikiSemanticLint } from './types'
import { WikiView } from './wiki'
import { WikiSettingsSection } from './wiki-settings'

vi.mock('./api')

const page: WikiPage = {
  page_id: 'topic_a', relative_path: 'topics/a.md', title: '待复核知识页', type: 'topic',
  revision: 1, body: '原始正文', tags: [], source_refs: [], backlinks: [], links: [], citation_refs: []
}

function Harness() {
  const [pageId, setPageId] = useState<string | null>(null)
  const [report, setReport] = useState<WikiSemanticLint | null>(null)

  return pageId
    ? <WikiView initialPageId={pageId} onOpenMedia={() => undefined} semanticReport={report} />
    : <WikiSettingsSection onOpenWiki={setPageId} onSemanticReport={setReport} semanticReport={report} />
}

describe('Wiki settings maintenance', () => {
  it('keeps semantic review available after navigating from settings into a clean Wiki view', async () => {
    vi.mocked(api.fetchWikiSettings).mockResolvedValue({ auto_ingest: true, queued_policy: 'continue' })
    vi.mocked(api.fetchWikiCatalog).mockResolvedValue({ initialized: true, items: [page], tags: [], types: ['topic'] })
    vi.mocked(api.fetchWikiPage).mockResolvedValue(page)
    vi.mocked(api.fetchWikiHistory).mockResolvedValue([])
    vi.mocked(api.fetchWikiIngestions).mockResolvedValue([])
    vi.mocked(api.fetchWikiDiff).mockResolvedValue({ changed: false, diff: '' } as Awaited<ReturnType<typeof api.fetchWikiDiff>>)
    vi.mocked(api.fetchStorageSettings).mockResolvedValue({ storage_root: 'D:\\vkc\\storage', migration: { phase: 'IDLE' } } as Awaited<ReturnType<typeof api.fetchStorageSettings>>)
    vi.mocked(api.lintWikiSemantics).mockResolvedValue({
      run_id: 'report-1', wiki_revision: 1, skill_sha256: 'hash',
      issues: [{ code: 'REVIEW', description: '需要复核', citations: [{ page_id: page.page_id, page_revision: 1 }] }]
    } as WikiSemanticLint)
    vi.mocked(api.applyWikiReview).mockResolvedValue({} as Awaited<ReturnType<typeof api.applyWikiReview>>)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

    render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>)
    const operations = ['关闭自动入库', '重建搜索索引', '预览历史补录', '补做知识融合', '结构巡检', '语义巡检', '规范影响预览', '修复目录索引']

    await screen.findByRole('button', { name: '关闭自动入库' })

    for (const name of operations) {expect(screen.getByRole('button', { name })).toBeTruthy()}
    fireEvent.click(screen.getByRole('button', { name: '语义巡检' }))
    fireEvent.click(await screen.findByRole('button', { name: '查看并复核 待复核知识页' }))
    await screen.findByRole('button', { name: '人工复核此页建议' })

    for (const name of operations) {expect(screen.queryByRole('button', { name })).toBeNull()}
    expect(screen.getByRole('button', { name: '向知识库提问' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '人工复核此页建议' }))
    fireEvent.change(screen.getByRole('textbox', { name: '复核后的页面正文' }), { target: { value: '复核后的正文' } })
    fireEvent.click(screen.getByRole('button', { name: '提交复核修订' }))
    await vi.waitFor(() => expect(api.applyWikiReview).toHaveBeenCalledWith('topic_a', 1, '复核后的正文', 'report-1'))
    client.clear()
  })
})
