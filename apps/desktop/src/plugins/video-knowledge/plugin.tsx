import {
  COMPOSER_AREAS,
  type ComposerMiddleware,
  type HermesPlugin,
  host,
  type RouteContribution,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  type SidebarNavContribution,
  TRANSCRIPT_DIRECTIVE_AREA,
  type TranscriptDirectiveContribution
} from '@hermes/plugin-sdk'

import { bindApi } from './api'
import {
  applyPendingVideoKnowledgeContext,
  VideoKnowledgeCitation,
  VideoKnowledgeContextBanner
} from './chat-context'
import { VIDEO_KNOWLEDGE_LOCALES } from './i18n'
import { VideoKnowledgePage } from './page'

const plugin: HermesPlugin = {
  id: 'video-knowledge',
  name: 'Video Knowledge',
  description: 'Collect video and build cited knowledge with Hermes.',
  defaultEnabled: true,
  register(ctx) {
    ctx.i18n.register(VIDEO_KNOWLEDGE_LOCALES)
    ctx.onDispose(bindApi(ctx.rest, ctx.socket))
    // The health call is also the lifecycle handshake: Hermes initializes the
    // profile-scoped database and supervised worker as soon as this bundled
    // plugin loads, before the user opens the page.
    const ensureRuntime = () => ctx.rest('/system/health').catch(() => undefined)
    void ensureRuntime()
    const runtimeHeartbeat = window.setInterval(ensureRuntime, 30_000)
    ctx.onDispose(() => window.clearInterval(runtimeHeartbeat))
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/video-knowledge' } satisfies RouteContribution,
        render: () => <VideoKnowledgePage />
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 45,
        data: {
          codicon: 'video',
          label: ctx.i18n.t('nav'),
          path: '/video-knowledge'
        } satisfies SidebarNavContribution
      },
      {
        id: 'chat-context-banner',
        area: COMPOSER_AREAS.top,
        render: () => <VideoKnowledgeContextBanner />
      },
      {
        id: 'chat-context-middleware',
        area: COMPOSER_AREAS.middleware,
        data: {
          handler: draft => applyPendingVideoKnowledgeContext(draft, host.state.focusedSessionId.get())
        } satisfies ComposerMiddleware
      },
      {
        id: 'video-citation',
        area: TRANSCRIPT_DIRECTIVE_AREA,
        data: {
          name: 'video-cite',
          render: props => <VideoKnowledgeCitation {...props} />
        } satisfies TranscriptDirectiveContribution
      }
    ])
  }
}

export default plugin
