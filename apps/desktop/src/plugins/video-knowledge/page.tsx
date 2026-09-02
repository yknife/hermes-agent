import { Badge, Codicon, Tabs, TabsList, TabsTrigger, useQuery } from '@hermes/plugin-sdk'
import { useState } from 'react'

import { AddContentView } from './add-content'
import { fetchHealth } from './api'
import { SystemSettingsView } from './asr-settings'
import { useVideoKnowledgeI18n } from './i18n'
import { JobsView } from './jobs'
import { LibraryView } from './library'

type View = 'add' | 'asr' | 'jobs' | 'library'

export function VideoKnowledgePage() {
  const t = useVideoKnowledgeI18n()
  const routeParams = new URLSearchParams(window.location.hash.split('?', 2)[1] ?? '')
  const routedMediaId = routeParams.get('media')
  const routedTimestamp = Number(routeParams.get('t'))
  const [view, setView] = useState<View>(routedMediaId ? 'library' : 'add')
  const [createdJobId, setCreatedJobId] = useState<null | string>(null)

  const health = useQuery({
    queryFn: fetchHealth,
    queryKey: ['video-knowledge', 'health'],
    refetchInterval: 10_000
  })

  return (
    <div className="flex h-full min-h-0 flex-col bg-background text-foreground">
      <header className="border-b border-(--ui-stroke-secondary) px-5 py-3">
        <div className="flex items-center justify-between gap-5">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Codicon className="text-primary" name="video" />
              <h1 className="text-base font-semibold">{t('title')}</h1>
            </div>
            <p className="mt-0.5 truncate text-xs text-muted-foreground">{t('subtitle')}</p>
          </div>
          <div className="flex items-center gap-3">
            <Tabs onValueChange={value => setView(value as View)} value={view}>
              <TabsList>
                <TabsTrigger value="add">
                  <Codicon name="add" />
                  添加内容
                </TabsTrigger>
                <TabsTrigger value="library">
                  <Codicon name="library" />
                  媒体库
                </TabsTrigger>
                <TabsTrigger value="jobs">
                  <Codicon name="list-unordered" />
                  任务中心
                </TabsTrigger>
                <TabsTrigger value="asr">
                  <Codicon name="settings-gear" />
                  系统设置
                </TabsTrigger>
              </TabsList>
            </Tabs>
            <Badge variant={health.data?.status === 'ok' ? 'default' : 'outline'}>
              {health.data?.status === 'ok' ? t('healthy') : t('degraded')}
            </Badge>
          </div>
        </div>
      </header>

      {view === 'add' && (
        <AddContentView
          onCreated={jobId => {
            setCreatedJobId(jobId)
            setView('jobs')
          }}
          onOpenSystemSettings={() => setView('asr')}
        />
      )}
      {view === 'library' && (
        <LibraryView
          initialMediaId={routedMediaId}
          initialSeekMs={Number.isFinite(routedTimestamp) && routedTimestamp >= 0 ? routedTimestamp : null}
        />
      )}
      {view === 'jobs' && <JobsView initialJobId={createdJobId} />}
      {view === 'asr' && <SystemSettingsView />}
    </div>
  )
}
