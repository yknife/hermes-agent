import { fireEvent, render, screen } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import type { WikiPage } from './types'
import { resolveWikiLink, WikiMarkdown } from './wiki-markdown'

const revision = `sr_${'a'.repeat(64)}`

const page: WikiPage = {
  page_id: 'video_a',
  relative_path: 'videos/a.md',
  type: 'video',
  title: 'A',
  revision: 1,
  tags: [],
  source_refs: [revision],
  backlinks: [],
  links: [{ href: '../sessions/live.md', page_id: 'session_live', title: '直播目录' }],
  citation_refs: [
    {
      item_key: '章节-1',
      source_revision: revision,
      media_id: 'a',
      transcript_id: 't',
      segment_ids: ['s1'],
      start_ms: 10000,
      end_ms: 12000
    }
  ],
  body:
    '# A\n<script>alert(1)</script>\n[恶意](javascript:alert(1))\n![私有文件](file:///C:/secret)\n[目录](../sessions/live.md)\n[证据 10.000s](../raw/videos/a/' +
    revision +
    '/transcript.md#segment-s1)'
}

describe('WikiMarkdown', () => {
  it('renders knowledge labels as bold text alongside verified citations', () => {
    const knowledgePage = {
      ...page,
      body: `- **材料事实**：材料事实（本次来源）；**推断·存在争议**：待核实 [证据 10.000s](../raw/videos/a/${revision}/transcript.md#segment-s1)`
    }

    const onCitation = vi.fn()

    const { container } = render(
      <WikiMarkdown onCitation={onCitation} onPage={() => undefined} page={knowledgePage} />
    )

    expect(screen.getByText('材料事实', { selector: 'strong' }).className).toContain('font-semibold')
    expect(screen.getByText('推断·存在争议', { selector: 'strong' }).className).toContain('font-semibold')
    expect(container.textContent).not.toContain('**')
    fireEvent.click(screen.getByRole('button', { name: '证据 10.000s' }))
    expect(onCitation).toHaveBeenCalledWith('章节-1')
  })

  it('keeps escaped or incomplete bold markers as text', () => {
    const knowledgePage = { ...page, body: '\\**保留星号**，**未闭合' }

    const html = renderToStaticMarkup(
      <WikiMarkdown onCitation={() => undefined} onPage={() => undefined} page={knowledgePage} />
    )

    expect(html).not.toContain('<strong')
    expect(html).toContain('**保留星号**')
    expect(html).toContain('**未闭合')
  })

  it('only activates known pages and verified citations', () => {
    expect(resolveWikiLink(page, 'javascript:alert(1)', '恶意')).toBeNull()
    expect(resolveWikiLink(page, '../sessions/live.md', '目录')).toEqual({ id: 'session_live', kind: 'page' })
    expect(resolveWikiLink(page, `../raw/videos/a/${revision}/transcript.md#segment-s1`, '证据 10.000s')).toEqual({
      id: '章节-1',
      kind: 'citation'
    })
  })

  it('renders raw HTML and unsafe links as inert text', () => {
    const unsafePage = { ...page, body: `${page.body}\n**<script>alert(2)</script>**` }

    const html = renderToStaticMarkup(
      <WikiMarkdown onCitation={() => undefined} onPage={() => undefined} page={unsafePage} />
    )

    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;')
    expect(html).toContain('<strong class="font-semibold">&lt;script&gt;alert(2)&lt;/script&gt;</strong>')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('href="javascript:')
    expect(html).not.toContain('<img')
    expect(html.match(/<button/g)?.length).toBe(2)
  })

  it('routes a time citation and page link through controlled callbacks', () => {
    const onCitation = vi.fn()
    const onPage = vi.fn()

    render(<WikiMarkdown onCitation={onCitation} onPage={onPage} page={page} />)
    fireEvent.click(screen.getByRole('button', { name: '证据 10.000s' }))
    fireEvent.click(screen.getByRole('button', { name: '目录' }))
    expect(onCitation).toHaveBeenCalledWith('章节-1')
    expect(onPage).toHaveBeenCalledWith('session_live')
  })

  it('keeps long pages readable through the final line', () => {
    const longPage = { ...page, body: Array.from({ length: 1000 }, (_, index) => `第 ${index} 段内容`).join('\n') }

    const html = renderToStaticMarkup(
      <WikiMarkdown onCitation={() => undefined} onPage={() => undefined} page={longPage} />
    )

    expect(html).toContain('第 999 段内容')
  })

  it('decodes escaped source text while keeping it inert', () => {
    const escaped = { ...page, body: 'A &amp; B，&lt;script&gt;不会执行&lt;/script&gt;' }

    const html = renderToStaticMarkup(
      <WikiMarkdown onCitation={() => undefined} onPage={() => undefined} page={escaped} />
    )

    expect(html).toContain('A &amp; B')
    expect(html).toContain('&lt;script&gt;不会执行&lt;/script&gt;')
    expect(html).not.toContain('<script>')
  })
})
