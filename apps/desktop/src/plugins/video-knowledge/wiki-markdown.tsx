import type { ReactNode } from 'react'

import type { WikiPage } from './types'

const LINK = /(!?)\[([^\]]+)\]\(([^)\s]+)\)/g
const RAW_CITATION = /^\.\.\/raw\/videos\/[^/]+\/(sr_[a-f0-9]{64})\/transcript\.md#segment-([^/?#]+)$/

function displayText(value: string): string {
  return value
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/\\\[/g, '[')
    .replace(/\\\]/g, ']')
}

export function resolveWikiLink(
  page: WikiPage,
  href: string,
  label: string
): null | { id: string; kind: 'citation' | 'page' } {
  const linkedPage = page.links.find(item => item.href === href)

  if (linkedPage) {
    return { id: linkedPage.page_id, kind: 'page' }
  }

  const source = RAW_CITATION.exec(href)

  if (!source) {
    return null
  }

  const seconds = /证据\s+([0-9]+(?:\.[0-9]+)?)s/.exec(label)
  const startMs = seconds ? Math.round(Number(seconds[1]) * 1000) : null

  const citation = page.citation_refs.find(
    ref =>
      ref.source_revision === source[1] &&
      ref.segment_ids[0] === source[2] &&
      (startMs === null || ref.start_ms === startMs)
  )

  return citation ? { id: citation.item_key, kind: 'citation' } : null
}

function inline(
  page: WikiPage,
  value: string,
  onPage: (pageId: string) => void,
  onCitation: (itemKey: string) => void
): ReactNode[] {
  const nodes: ReactNode[] = []
  let previous = 0

  for (const match of value.matchAll(LINK)) {
    const index = match.index ?? 0

    if (index > previous) {
      nodes.push(displayText(value.slice(previous, index)))
    }

    const [, image, label, href] = match
    const target = image ? null : resolveWikiLink(page, href, label)

    if (target) {
      nodes.push(
        <button
          className="font-medium text-primary underline underline-offset-2 hover:opacity-75"
          key={`${index}-${href}`}
          onClick={() => (target.kind === 'page' ? onPage(target.id) : onCitation(target.id))}
          type="button"
        >
          {displayText(label)}
        </button>
      )
    } else {
      nodes.push(
        <span className="text-muted-foreground" key={`${index}-${href}`} title="此链接不能在知识库中打开">
          {displayText(label)}
        </span>
      )
    }

    previous = index + match[0].length
  }

  if (previous < value.length) {
    nodes.push(displayText(value.slice(previous)))
  }

  return nodes
}

export function WikiMarkdown({
  page,
  onPage,
  onCitation
}: {
  page: WikiPage
  onPage: (pageId: string) => void
  onCitation: (itemKey: string) => void
}) {
  const lines = page.body.split('\n')
  let inCode = false

  return (
    <div className="space-y-2 text-sm leading-7 wrap-anywhere" data-selectable-text="true">
      {lines.map((line, index) => {
        if (line.startsWith('```')) {
          inCode = !inCode

          return null
        }

        if (inCode) {
          return (
            <pre className="overflow-x-auto rounded bg-muted p-2 font-mono text-xs" key={index}>
              {line}
            </pre>
          )
        }

        if (!line.trim()) {
          return <div aria-hidden="true" className="h-1" key={index} />
        }

        const heading = /^(#{1,6})\s+(.+)$/.exec(line)

        if (heading) {
          const level = heading[1].length

          return (
            <div
              className={
                level === 1
                  ? 'pt-2 text-xl font-semibold'
                  : level === 2
                    ? 'pt-4 text-base font-semibold'
                    : 'pt-2 font-semibold'
              }
              key={index}
            >
              {inline(page, heading[2], onPage, onCitation)}
            </div>
          )
        }

        const bullet = /^\s*-\s+(.+)$/.exec(line)

        if (bullet) {
          return (
            <div className="pl-4 before:mr-2 before:content-['•']" key={index}>
              {inline(page, bullet[1], onPage, onCitation)}
            </div>
          )
        }

        const quote = /^>\s*(.+)$/.exec(line)

        if (quote) {
          return (
            <blockquote className="border-l-2 border-amber-500 pl-3 text-amber-700 dark:text-amber-300" key={index}>
              {inline(page, quote[1], onPage, onCitation)}
            </blockquote>
          )
        }

        return <p key={index}>{inline(page, line, onPage, onCitation)}</p>
      })}
    </div>
  )
}
