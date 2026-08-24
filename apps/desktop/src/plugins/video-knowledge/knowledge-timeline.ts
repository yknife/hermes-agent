import type { KnowledgeDocument } from './types'

export type KnowledgeTimelineKind = 'chapter' | 'knowledge' | 'question'

export interface KnowledgeTimelineItem {
  body: string
  endMs: number
  id: string
  kind: KnowledgeTimelineKind
  label: string
  startMs: number
  title: string
}

export interface KnowledgeTimeline {
  items: KnowledgeTimelineItem[]
  summary: null | string
}

interface Citation {
  end_ms: number
  start_ms: number
}

const knowledgeTypeLabels: Record<string, string> = {
  action_item: '行动建议',
  claim: '核心观点',
  concept: '关键概念',
  evidence: '事实依据'
}

function record(value: unknown): null | Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function text(value: unknown): null | string {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function citation(value: unknown): Citation | null {
  const candidate = record(value)

  if (!candidate || typeof candidate.start_ms !== 'number' || typeof candidate.end_ms !== 'number') {
    return null
  }

  if (candidate.start_ms < 0 || candidate.end_ms < candidate.start_ms) {
    return null
  }

  return { end_ms: candidate.end_ms, start_ms: candidate.start_ms }
}

export function buildKnowledgeTimeline(documents: KnowledgeDocument[]): KnowledgeTimeline {
  let summary: null | string = null
  const items: Array<KnowledgeTimelineItem & { order: number }> = []
  let order = 0

  for (const document of documents) {
    if (document.document_type === 'summary') {
      const content = record(document.content)

      summary = text(content?.summary) ?? text(document.content) ?? summary

      continue
    }

    if (!Array.isArray(document.content)) {
      continue
    }

    document.content.forEach((value, index) => {
      const content = record(value)
      const source = citation(content?.citation)

      if (!content || !source) {
        return
      }

      const id = `${document.id}-${index}`

      if (document.document_type === 'chapters') {
        const title = text(content.title)
        const body = text(content.summary)

        if (title && body) {
          items.push({ body, endMs: source.end_ms, id, kind: 'chapter', label: '章节', order: order++, startMs: source.start_ms, title })
        }
      } else if (document.document_type === 'knowledge_points') {
        const title = text(content.title)
        const body = text(content.content)

        if (title && body) {
          const type = text(content.type) ?? ''
          items.push({ body, endMs: source.end_ms, id, kind: 'knowledge', label: knowledgeTypeLabels[type] ?? '知识点', order: order++, startMs: source.start_ms, title })
        }
      } else if (document.document_type === 'suggested_qa') {
        const title = text(content.question)
        const body = text(content.answer)

        if (title && body) {
          items.push({ body, endMs: source.end_ms, id, kind: 'question', label: '相关问答', order: order++, startMs: source.start_ms, title })
        }
      }
    })
  }

  return {
    items: items
      .sort((left, right) => left.startMs - right.startMs || left.order - right.order)
      .map(({ order: _order, ...item }) => item),
    summary
  }
}
