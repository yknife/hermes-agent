import type { KnowledgeDocument } from './types'

export type KnowledgeTimelineKind = 'chapter' | 'degraded' | 'knowledge' | 'question'

export interface KnowledgeTimelineItem {
  body: string
  degraded: boolean
  degradationReason: null | string
  endMs: number
  id: string
  kind: KnowledgeTimelineKind
  label: string
  startMs: number
  title: string
}

export interface KnowledgeDegradedRange {
  endMs: number
  id: string
  reason: string
  startMs: number
}

export interface KnowledgeTimeline {
  degradedRanges: KnowledgeDegradedRange[]
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

const degradationReasons: Record<string, string> = {
  model_invalid_response: '模型未返回有效的结构化分析，系统已改用原始字幕摘录兜底。'
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

function isLegacyFallback(documentType: string, content: Record<string, unknown>): boolean {
  const title = text(content.title) ?? ''
  const question = text(content.question) ?? ''

  return (documentType === 'chapters' && /^Transcript section \d/.test(title))
    || (documentType === 'knowledge_points' && /^Transcript evidence \d/.test(title))
    || (documentType === 'suggested_qa' && /^What is discussed in transcript section \d/.test(question))
}

function reasonLabel(reason: null | string): string {
  return reason ? degradationReasons[reason] ?? reason : degradationReasons.model_invalid_response
}

function overlaps(range: KnowledgeDegradedRange, source: Citation): boolean {
  // Transcript segments commonly share an end/start boundary. Treat ranges
  // as half-open here so the first item after a degraded chunk is not marked.
  return source.start_ms < range.endMs && source.end_ms > range.startMs
}

export function buildKnowledgeTimeline(documents: KnowledgeDocument[]): KnowledgeTimeline {
  let summary: null | string = null
  const degradedRanges: KnowledgeDegradedRange[] = []
  const items: Array<KnowledgeTimelineItem & { order: number }> = []
  let order = 0

  for (const document of documents) {
    if (document.document_type !== 'summary') {
      continue
    }

    const content = record(document.content)
    summary = text(content?.summary) ?? text(document.content) ?? summary
    const ranges = content && Array.isArray(content.degraded_ranges) ? content.degraded_ranges : []
    ranges.forEach((value, index) => {
      const range = record(value)
      const source = citation(range?.citation)

      if (source) {
        degradedRanges.push({
          endMs: source.end_ms,
          id: `${document.id}-degraded-${index}`,
          reason: text(range?.reason) ?? 'model_invalid_response',
          startMs: source.start_ms
        })
      }
    })
  }

  for (const document of documents) {
    if (document.document_type === 'summary' || !Array.isArray(document.content)) {
      continue
    }

    document.content.forEach((value, index) => {
      const content = record(value)
      const source = citation(content?.citation)

      if (!content || !source) {
        return
      }

      const legacyFallback = isLegacyFallback(document.document_type, content)
      const explicitlyDegraded = content.degraded === true
      const degradationReason = text(content.degradation_reason) ?? (legacyFallback ? 'model_invalid_response' : null)

      if ((explicitlyDegraded || legacyFallback) && !degradedRanges.some(range => overlaps(range, source))) {
        degradedRanges.push({
          endMs: source.end_ms,
          id: `${document.id}-legacy-degraded-${index}`,
          reason: degradationReason ?? 'model_invalid_response',
          startMs: source.start_ms
        })
      }

      const id = `${document.id}-${index}`
      const degraded = explicitlyDegraded || legacyFallback || degradedRanges.some(range => overlaps(range, source))

      const shared = {
        degraded,
        degradationReason: degraded ? reasonLabel(degradationReason) : null,
        endMs: source.end_ms,
        id,
        order: order++,
        startMs: source.start_ms
      }

      if (document.document_type === 'chapters') {
        const title = text(content.title)
        const body = text(content.summary)

        if (title && body) {
          items.push({ ...shared, body, kind: 'chapter', label: '章节', title })
        }
      } else if (document.document_type === 'knowledge_points') {
        const title = text(content.title)
        const body = text(content.content)

        if (title && body) {
          const type = text(content.type) ?? ''
          items.push({ ...shared, body, kind: 'knowledge', label: knowledgeTypeLabels[type] ?? '知识点', title })
        }
      } else if (document.document_type === 'suggested_qa') {
        const title = text(content.question)
        const body = text(content.answer)

        if (title && body) {
          items.push({ ...shared, body, kind: 'question', label: '相关问答', title })
        }
      }
    })
  }

  const uniqueRanges = [...new Map(
    degradedRanges.map(range => [`${range.startMs}:${range.endMs}`, range])
  ).values()].sort((left, right) => left.startMs - right.startMs || left.endMs - right.endMs)

  uniqueRanges.forEach(range => {
    items.push({
      body: reasonLabel(range.reason),
      degraded: true,
      degradationReason: reasonLabel(range.reason),
      endMs: range.endMs,
      id: range.id,
      kind: 'degraded',
      label: '兜底区间',
      order: order++,
      startMs: range.startMs,
      title: '此时间段使用了降级分析'
    })
  })

  return {
    degradedRanges: uniqueRanges,
    items: items
      .sort((left, right) => left.startMs - right.startMs || left.order - right.order)
      .map(({ order: _order, ...item }) => item),
    summary
  }
}
