import { type PluginLocaleBundles, usePluginI18n } from '@hermes/plugin-sdk'

const en = {
  nav: 'Video Knowledge',
  title: 'Video Knowledge',
  subtitle: 'Collect media, transcribe it, and build cited knowledge with Hermes.',
  url: 'Video URL',
  collect: 'Collect',
  collecting: 'Adding…',
  library: 'Library',
  jobs: 'Jobs',
  knowledge: 'Knowledge',
  emptyMedia: 'No videos yet',
  emptyMediaBody: 'Paste a supported video URL to begin.',
  emptyKnowledge: 'Knowledge is being prepared',
  analyze: 'Analyze again',
  healthy: 'Ready',
  degraded: 'Needs attention',
  failed: 'Could not add this video'
}

const zh = {
  nav: '视频知识',
  title: '视频知识采集',
  subtitle: '采集视频、生成文字稿，并由 Hermes 提炼带引用的知识。',
  url: '视频地址',
  collect: '开始采集',
  collecting: '正在添加…',
  library: '资料库',
  jobs: '任务',
  knowledge: '知识',
  emptyMedia: '还没有视频',
  emptyMediaBody: '粘贴支持的视频地址即可开始。',
  emptyKnowledge: '知识正在生成',
  analyze: '重新分析',
  healthy: '运行正常',
  degraded: '需要关注',
  failed: '视频添加失败'
}

const zhHant = {
  nav: '影片知識',
  title: '影片知識採集',
  subtitle: '採集影片、生成文字稿，並由 Hermes 提煉附引用的知識。',
  url: '影片網址',
  collect: '開始採集',
  collecting: '正在加入…',
  library: '資料庫',
  jobs: '任務',
  knowledge: '知識',
  emptyMedia: '尚無影片',
  emptyMediaBody: '貼上支援的影片網址即可開始。',
  emptyKnowledge: '知識正在生成',
  analyze: '重新分析',
  healthy: '運作正常',
  degraded: '需要注意',
  failed: '影片加入失敗'
}

const ja = {
  nav: '動画ナレッジ',
  title: '動画ナレッジ収集',
  subtitle: '動画を収集・文字起こしし、Hermes で引用付き知識を生成します。',
  url: '動画 URL',
  collect: '収集を開始',
  collecting: '追加中…',
  library: 'ライブラリ',
  jobs: 'ジョブ',
  knowledge: 'ナレッジ',
  emptyMedia: '動画はまだありません',
  emptyMediaBody: '対応する動画 URL を貼り付けてください。',
  emptyKnowledge: 'ナレッジを生成しています',
  analyze: '再分析',
  healthy: '準備完了',
  degraded: '確認が必要',
  failed: '動画を追加できませんでした'
}

const ar = {
  nav: 'معرفة الفيديو',
  title: 'جامع معرفة الفيديو',
  subtitle: 'اجمع الفيديوهات وانسخها وحوّلها إلى معرفة موثقة عبر Hermes.',
  url: 'رابط الفيديو',
  collect: 'بدء الجمع',
  collecting: 'جارٍ الإضافة…',
  library: 'المكتبة',
  jobs: 'المهام',
  knowledge: 'المعرفة',
  emptyMedia: 'لا توجد فيديوهات بعد',
  emptyMediaBody: 'ألصق رابط فيديو مدعومًا للبدء.',
  emptyKnowledge: 'جارٍ إعداد المعرفة',
  analyze: 'إعادة التحليل',
  healthy: 'جاهز',
  degraded: 'يحتاج إلى الانتباه',
  failed: 'تعذرت إضافة الفيديو'
}

export const VIDEO_KNOWLEDGE_LOCALES: PluginLocaleBundles = { en, zh, 'zh-hant': zhHant, ja, ar }
export const useVideoKnowledgeI18n = () => usePluginI18n('video-knowledge')
