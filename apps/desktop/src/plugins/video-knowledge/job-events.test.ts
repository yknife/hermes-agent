import { describe, expect, it } from 'vitest'

import { collapseProgressEvents } from './job-events'
import type { JobEvent } from './types'

function event(
  id: string,
  type: string,
  message: string,
  stage = 'RECORDING'
): JobEvent {
  return {
    data: {
      job_id: 'job-live',
      message,
      overall_progress: Number(id),
      stage
    },
    event_id: id,
    occurred_at: `2026-08-23T00:00:0${id}Z`,
    type
  }
}

describe('collapseProgressEvents', () => {
  it('keeps only the newest event from identical consecutive recording updates', () => {
    const collapsed = collapseProgressEvents([
      event('1', 'job.progress', '正在录制直播，已录制 1 秒'),
      event('2', 'job.progress', '正在录制直播，已录制 2 秒'),
      event('3', 'job.progress', '正在录制直播，已录制 3 秒')
    ])

    expect(collapsed.map(value => value.event_id)).toEqual(['3'])
  })

  it('preserves state changes and separate progress runs', () => {
    const collapsed = collapseProgressEvents([
      event('1', 'job.progress', '正在录制直播'),
      event('2', 'job.state_changed', '等待重连'),
      event('3', 'job.progress', '正在录制直播'),
      event('4', 'job.progress', '正在合并分片', 'VERIFYING_MEDIA')
    ])

    expect(collapsed.map(value => value.event_id)).toEqual(['1', '2', '3', '4'])
  })

  it('keeps only the newest event from repeated offline live polling', () => {
    const collapsed = collapseProgressEvents([
      event('1', 'job.state_changed', '等待时间结束，重新进入队列', 'MONITORING_LIVE'),
      event('2', 'job.state_changed', 'Worker worker-1 已领取任务', 'MONITORING_LIVE'),
      event('3', 'job.progress', '正在检测直播状态', 'MONITORING_LIVE'),
      event('4', 'job.state_changed', '直播尚未开播，等待下次检测', 'MONITORING_LIVE'),
      event('5', 'job.state_changed', '等待时间结束，重新进入队列', 'MONITORING_LIVE'),
      event('6', 'job.state_changed', 'Worker worker-1 已领取任务', 'MONITORING_LIVE'),
      event('7', 'job.progress', '正在检测直播状态', 'MONITORING_LIVE'),
      event('8', 'job.state_changed', '直播尚未开播，等待下次检测', 'MONITORING_LIVE')
    ])

    expect(collapsed.map(value => value.event_id)).toEqual(['8'])
  })

  it('keeps only the newest event from repeated local import progress', () => {
    const collapsed = collapseProgressEvents([
      event('1', 'job.progress', '正在导入本地视频', 'ACQUIRING_MEDIA'),
      event('2', 'job.progress', '正在导入本地视频', 'ACQUIRING_MEDIA'),
      event('3', 'job.progress', '正在导入本地视频', 'ACQUIRING_MEDIA'),
      event('4', 'job.progress', '正在校验媒体完整性', 'VERIFYING_MEDIA')
    ])

    expect(collapsed.map(value => value.event_id)).toEqual(['3', '4'])
  })
})
