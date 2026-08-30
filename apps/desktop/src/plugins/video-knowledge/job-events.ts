import type { JobEvent } from './types'

function isLiveRecordingProgress(event: JobEvent): boolean {
  return (
    event.type === 'job.progress' &&
    event.data.stage === 'RECORDING' &&
    event.data.message?.startsWith('正在录制直播') === true
  )
}

function isSameProgress(previous: JobEvent, current: JobEvent): boolean {
  return (
    previous.type === 'job.progress'
    && current.type === 'job.progress'
    && previous.data.stage === current.data.stage
    && previous.data.message === current.data.message
  )
}

function isLiveMonitorPollEvent(event: JobEvent): boolean {
  if (event.data.stage !== 'MONITORING_LIVE') {
    return false
  }

  const message = event.data.message ?? ''

  return (
    message === '正在检测直播状态'
    || message === '等待时间结束，重新进入队列'
    || message === '直播尚未开播，等待下次检测'
    || (message.startsWith('Worker ') && message.endsWith(' 已领取任务'))
  )
}

/** Keep the newest event from each consecutive run of identical progress text. */
export function collapseProgressEvents(events: JobEvent[]): JobEvent[] {
  return events.reduce<JobEvent[]>((collapsed, event) => {
    const previous = collapsed.at(-1)

    if (
      previous
      && (
        isSameProgress(previous, event)
        || (isLiveRecordingProgress(previous) && isLiveRecordingProgress(event))
        || (isLiveMonitorPollEvent(previous) && isLiveMonitorPollEvent(event))
      )
    ) {
      collapsed[collapsed.length - 1] = event
    } else {
      collapsed.push(event)
    }

    return collapsed
  }, [])
}
