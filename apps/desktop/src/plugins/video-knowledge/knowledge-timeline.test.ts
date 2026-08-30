import { describe, expect, it } from 'vitest'

import { buildKnowledgeTimeline } from './knowledge-timeline'
import type { KnowledgeDocument } from './types'

function document(document_type: string, content: unknown, id = document_type): KnowledgeDocument {
  return { content, document_type, id, model: 'test-model', version: 1 }
}

describe('knowledge timeline', () => {
  it('combines customer-facing knowledge into video time order', () => {
    const result = buildKnowledgeTimeline([
      document('summary', { summary: '视频概览' }),
      document('chapters', [{
        citation: { end_ms: 16_000, segment_ids: ['s2'], start_ms: 10_000 },
        summary: '章节内容',
        title: '第二部分'
      }]),
      document('knowledge_points', [{
        citation: { end_ms: 6_000, segment_ids: ['s1'], start_ms: 5_000 },
        confidence: 0.9,
        content: '先出现的观点',
        title: '关键结论',
        type: 'claim'
      }]),
      document('suggested_qa', [{
        answer: '问题答案',
        citation: { end_ms: 13_000, segment_ids: ['s3'], start_ms: 12_000 },
        question: '用户会问什么？'
      }])
    ])

    expect(result.summary).toBe('视频概览')
    expect(result.degradedRanges).toEqual([])
    expect(result.items.map(item => [item.startMs, item.label, item.title])).toEqual([
      [5_000, '核心观点', '关键结论'],
      [10_000, '章节', '第二部分'],
      [12_000, '相关问答', '用户会问什么？']
    ])
  })

  it('ignores malformed generated entries instead of breaking the view', () => {
    const result = buildKnowledgeTimeline([
      document('summary', '兼容旧版摘要'),
      document('chapters', [null, { summary: '没有引用', title: '无效章节' }]),
      document('knowledge_points', 'not-an-array')
    ])

    expect(result).toEqual({ degradedRanges: [], items: [], summary: '兼容旧版摘要' })
  })

  it('marks structured fallback ranges and their overlapping timeline entries', () => {
    const result = buildKnowledgeTimeline([
      document('summary', {
        degraded: true,
        degraded_ranges: [{
          chunk_index: 2,
          citation: { end_ms: 48_000, segment_ids: ['s2', 's3'], start_ms: 24_000 },
          reason: 'model_invalid_response'
        }],
        summary: '部分内容使用字幕兜底'
      }),
      document('chapters', [{
        citation: { end_ms: 40_000, segment_ids: ['s2'], start_ms: 30_000 },
        summary: '字幕摘录',
        title: '模型汇总后的章节'
      }, {
        citation: { end_ms: 55_000, segment_ids: ['s4'], start_ms: 48_000 },
        summary: '正常分析',
        title: '紧邻降级区间的章节'
      }])
    ])

    expect(result.degradedRanges).toEqual([expect.objectContaining({ endMs: 48_000, startMs: 24_000 })])
    expect(result.items.map(item => [item.kind, item.degraded, item.startMs])).toEqual([
      ['degraded', true, 24_000],
      ['chapter', true, 30_000],
      ['chapter', false, 48_000]
    ])
    expect(result.items[0]?.label).toBe('兜底区间')
  })

  it('recognizes legacy transcript fallback results without reanalysis', () => {
    const result = buildKnowledgeTimeline([
      document('summary', { summary: '旧结果' }),
      document('knowledge_points', [{
        citation: { end_ms: 18_000, segment_ids: ['s1'], start_ms: 12_000 },
        confidence: 1,
        content: 'verbatim transcript',
        title: 'Transcript evidence 3.1',
        type: 'evidence'
      }])
    ])

    expect(result.degradedRanges).toHaveLength(1)
    expect(result.items.every(item => item.degraded)).toBe(true)
  })
})
