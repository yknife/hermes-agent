import {
  Badge,
  Button,
  Checkbox,
  Codicon,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
  host,
  Input,
  ModelCatalogMenu,
  ModelMenuCloseContext,
  type ModelMenuController,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'

import {
  createLiveSource,
  fetchAsrStatus,
  fetchStorageSettings,
  ingest,
  ingestLocal,
  probeSource
} from './api'
import { durationLabel, errorCode, errorMessage } from './format'
import type { IngestOptions, LiveSourceOptions, LocalIngestOptions } from './types'

const MODELS = [
  { label: 'tiny（最快，低资源）', value: 'tiny' },
  { label: 'base（快速）', value: 'base' },
  { label: 'small（推荐）', value: 'small' },
  { label: 'medium（更准确）', value: 'medium' },
  { label: 'large-v3（最高精度，高配置）', value: 'large-v3' },
  { label: 'large-v3-turbo（更快、更省资源）', value: 'large-v3-turbo' }
]

export function AddContentView({
  onCreated,
  onOpenSystemSettings
}: {
  onCreated: (jobId: string) => void
  onOpenSystemSettings: () => void
}) {
  const queryClient = useQueryClient()
  const [sourceMode, setSourceMode] = useState<'link' | 'local'>('link')
  const [url, setUrl] = useState('')
  const [cookiesPath, setCookiesPath] = useState('')
  const [requiresCookies, setRequiresCookies] = useState(false)
  const [localPath, setLocalPath] = useState('')
  const [localTitle, setLocalTitle] = useState('')
  const [localAuthor, setLocalAuthor] = useState('')
  const [maxHeight, setMaxHeight] = useState('1080')
  const [subtitleLanguages, setSubtitleLanguages] = useState('zh-CN,zh,en')
  const [pollInterval, setPollInterval] = useState('120')
  const [quality, setQuality] = useState<LiveSourceOptions['quality_policy']>('OD')
  const [maxMinutes, setMaxMinutes] = useState('240')
  const [asrEnabled, setAsrEnabled] = useState(true)
  const [asrModel, setAsrModel] = useState('small')
  const [asrDevice, setAsrDevice] = useState<'auto' | 'cpu' | 'cuda'>('auto')
  const [computeType, setComputeType] = useState('auto')
  const [language, setLanguage] = useState('')
  const [vadFilter, setVadFilter] = useState(true)
  const [wordTimestamps, setWordTimestamps] = useState(false)
  const [autoAnalyze, setAutoAnalyze] = useState(true)
  const [analysisSelection, setAnalysisSelection] = useState<null | { model: string; provider: string }>(null)
  const [analysisModelPickerOpen, setAnalysisModelPickerOpen] = useState(false)
  const [defaultsApplied, setDefaultsApplied] = useState(false)
  const asrStatus = useQuery({ queryFn: fetchAsrStatus, queryKey: ['video-knowledge', 'system', 'asr'] })

  const storageSettings = useQuery({
    queryFn: fetchStorageSettings,
    queryKey: ['video-knowledge', 'system', 'storage']
  })

  const probe = useMutation({
    mutationFn: ({ cookiesFile, sourceUrl }: { cookiesFile: null | string; sourceUrl: string }) => (
      probeSource(sourceUrl, cookiesFile)
    ),
    onError: error => {
      if (errorCode(error) === 'AUTH_REQUIRED') {
        setRequiresCookies(true)
      }
    }
  })

  const analysisModelController: ModelMenuController = {
    applyPreset: (_preset, row) => setAnalysisSelection(row),
    current: {
      effort: '',
      fast: false,
      model: analysisSelection?.model ?? '',
      provider: analysisSelection?.provider ?? ''
    },
    presetFor: () => ({}),
    select: (model, provider) => setAnalysisSelection({ model, provider }),
    setOptions: (_patch, row) => setAnalysisSelection({ model: row.model, provider: row.provider })
  }

  useEffect(() => {
    if (!asrStatus.data || defaultsApplied) {
      return
    }

    setDefaultsApplied(true)
    setAsrEnabled(asrStatus.data.enabled)
    setAsrModel(asrStatus.data.model)
    setAsrDevice(asrStatus.data.configured_device as typeof asrDevice)
    setComputeType(asrStatus.data.configured_compute_type)
    setLanguage(asrStatus.data.language ?? '')
    setVadFilter(asrStatus.data.vad_filter)
    setWordTimestamps(asrStatus.data.word_timestamps)
    setAutoAnalyze(asrStatus.data.auto_analyze)
  }, [asrStatus.data, defaultsApplied])

  const collect = useMutation({
    mutationFn: async () => {
      const asrOptions = {
        asr_compute_type: computeType,
        asr_device: asrDevice,
        asr_enabled: asrEnabled,
        asr_language: language.trim() || null,
        asr_model: asrModel,
        asr_vad_filter: vadFilter,
        asr_word_timestamps: wordTimestamps,
        analysis_model: analysisSelection?.model ?? null,
        analysis_provider: analysisSelection?.provider ?? null
      }

      if (sourceMode === 'local') {
        if (!localPath.trim()) {
          throw new Error('请输入或选择本地视频文件')
        }

        if (!localTitle.trim()) {
          throw new Error('请输入视频标题')
        }

        const options: LocalIngestOptions = {
          ...asrOptions,
          auto_analyze: autoAnalyze
        }

        return ingestLocal(localPath.trim(), localTitle.trim(), localAuthor.trim(), options)
      }

      if (!probe.data) {
        throw new Error('请先识别链接')
      }

      if (probe.data.source_type === 'LIVE') {
        return createLiveSource(url.trim(), {
          ...asrOptions,
          auto_analyze: autoAnalyze,
          poll_interval_seconds: Number(pollInterval),
          quality_policy: quality,
          reconnect_attempts: 3,
          reconnect_delay_seconds: 5,
          recording_max_seconds: Number(maxMinutes) * 60
        })
      }

      const options: IngestOptions = {
        ...asrOptions,
        auto_analyze: autoAnalyze,
        cookies_file: cookiesPath.trim() || null,
        max_height: Number(maxHeight),
        subtitle_languages: subtitleLanguages
          .split(',')
          .map(value => value.trim())
          .filter(Boolean)
      }

      return ingest(url.trim(), options)
    },
    onSuccess: result => {
      void queryClient.invalidateQueries({ queryKey: ['video-knowledge'] })
      onCreated(result.job.id)
    }
  })

  const pickLocalFile = useMutation({
    mutationFn: () => host.selectPaths({
      filters: [{ name: '视频文件', extensions: ['mp4', 'mkv', 'mov', 'avi', 'webm', 'm4v', 'flv', 'ts', 'mpeg', 'mpg', 'wmv'] }],
      multiple: false,
      title: '选择本地视频'
    }),
    onSuccess: paths => {
      const path = paths[0]

      if (!path) {
        return
      }

      setLocalPath(path)
      setLocalTitle(current => current.trim() || titleFromPath(path))
      collect.reset()
    }
  })

  const pickCookiesFile = useMutation({
    mutationFn: () => host.selectPaths({
      filters: [{ name: 'Cookies 文件', extensions: ['txt'] }],
      multiple: false,
      title: '选择 Netscape Cookies 文件'
    }),
    onSuccess: paths => {
      const path = paths[0]

      if (!path) {
        return
      }

      setCookiesPath(path)
      probe.reset()
      collect.reset()
    }
  })

  const runProbe = () => {
    if (!url.trim()) {
      return
    }

    collect.reset()
    probe.mutate({
      cookiesFile: cookiesPath.trim() || null,
      sourceUrl: url.trim()
    })
  }

  const sourceType = sourceMode === 'local' ? 'VIDEO' : probe.data?.source_type
  const localReady = Boolean(localPath.trim() && localTitle.trim())

  return (
    <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-auto p-5 xl:grid-cols-[minmax(22rem,1fr)_minmax(20rem,0.9fr)]">
      <section className="rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-5">
        <div className="mb-5">
          <div className="text-[0.6875rem] font-semibold uppercase tracking-wider text-primary">统一内容入口</div>
          <h2 className="mt-1 text-base font-semibold">添加链接或本地视频</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            链接支持视频和直播；本地视频直接复用 ASR、Transcript 与 Hermes 知识分析。
          </p>
        </div>

        <form
          className="space-y-5"
          onSubmit={event => {
            event.preventDefault()

            if (sourceMode === 'local') {
              collect.mutate()
            } else {
              runProbe()
            }
          }}
        >
          <div
            aria-label="内容来源"
            className="grid grid-cols-2 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-quaternary) p-1"
            role="group"
          >
            <Button
              aria-pressed={sourceMode === 'link'}
              onClick={() => {
                setSourceMode('link')
                collect.reset()
              }}
              type="button"
              variant={sourceMode === 'link' ? 'secondary' : 'ghost'}
            >
              <Codicon name="link" />
              内容链接
            </Button>
            <Button
              aria-pressed={sourceMode === 'local'}
              onClick={() => {
                setSourceMode('local')
                setCookiesPath('')
                setRequiresCookies(false)
                probe.reset()
                collect.reset()
              }}
              type="button"
              variant={sourceMode === 'local' ? 'secondary' : 'ghost'}
            >
              <Codicon name="folder-opened" />
              本地视频
            </Button>
          </div>

          <div className="rounded-md border border-amber-500/35 bg-amber-500/10 p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 text-xs font-semibold">
                  <Codicon className="text-amber-600 dark:text-amber-300" name="folder-opened" />
                  当前媒体资产存储路径
                </div>
                <div className="mt-2 break-all rounded bg-background/50 px-3 py-2 font-mono text-[0.6875rem] text-(--ui-text-secondary)">
                  {storageSettings.isLoading
                    ? '正在读取存储配置…'
                    : storageSettings.data?.storage_root ?? '暂时无法读取存储路径'}
                </div>
                <p className="mt-2 text-[0.6875rem] leading-5 text-muted-foreground">
                  内容链接和本地视频共用此目录。建议第一次添加内容前先修改到空间充足的磁盘，避免视频、封面、ASR 音频和 Transcript 持续占用 C 盘。
                </p>
                {storageSettings.isError && (
                  <div className="mt-2 text-[0.6875rem] text-destructive">
                    {errorMessage(storageSettings.error, '读取存储路径失败')}
                  </div>
                )}
              </div>
              <Button onClick={onOpenSystemSettings} size="sm" type="button" variant="secondary">
                <Codicon name="settings-gear" />
                前往系统设置
              </Button>
            </div>
          </div>

          {sourceMode === 'link' ? (
            <div className="space-y-4">
              <Field label="内容链接">
                <div className="flex gap-2">
                  <Input
                    aria-label="内容链接"
                    onChange={event => {
                      setUrl(event.target.value)
                      setCookiesPath('')
                      setRequiresCookies(false)
                      probe.reset()
                      collect.reset()
                    }}
                    placeholder="视频地址或直播间地址"
                    type="url"
                    value={url}
                  />
                  <Button disabled={!url.trim() || probe.isPending} type="submit">
                    <Codicon name="search" />
                    {probe.isPending ? '识别中…' : '识别链接'}
                  </Button>
                </div>
              </Field>
              {requiresCookies && (
                <Field label="Cookies 文件">
                  <div className="space-y-1.5">
                    <div className="flex gap-2">
                      <Input
                        aria-label="Cookies 文件路径"
                        placeholder="请选择 Netscape 格式的 cookies.txt"
                        readOnly
                        value={cookiesPath}
                      />
                      <Button
                        disabled={pickCookiesFile.isPending}
                        onClick={() => pickCookiesFile.mutate()}
                        type="button"
                        variant="secondary"
                      >
                        <Codicon name="folder-opened" />
                        {pickCookiesFile.isPending ? '选择中…' : '选择文件'}
                      </Button>
                    </div>
                    <div className="font-normal text-muted-foreground">
                      仅用于当前链接的识别和采集；选择后请重新点击“识别链接”。
                    </div>
                  </div>
                </Field>
              )}
            </div>
          ) : (
            <div className="space-y-4">
              <Field label="本地视频路径">
                <div className="flex gap-2">
                  <Input
                    aria-label="本地视频路径"
                    onChange={event => {
                      setLocalPath(event.target.value)
                      collect.reset()
                    }}
                    placeholder="填写路径或选择本地视频文件"
                    type="text"
                    value={localPath}
                  />
                  <Button
                    disabled={pickLocalFile.isPending}
                    onClick={() => pickLocalFile.mutate()}
                    type="button"
                    variant="secondary"
                  >
                    <Codicon name="folder-opened" />
                    {pickLocalFile.isPending ? '选择中…' : '选择文件'}
                  </Button>
                </div>
              </Field>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <Field label="视频标题">
                  <Input
                    maxLength={500}
                    onChange={event => {
                      setLocalTitle(event.target.value)
                      collect.reset()
                    }}
                    placeholder="必填"
                    value={localTitle}
                  />
                </Field>
                <Field label="视频作者">
                  <Input
                    maxLength={500}
                    onChange={event => {
                      setLocalAuthor(event.target.value)
                      collect.reset()
                    }}
                    placeholder="可选"
                    value={localAuthor}
                  />
                </Field>
              </div>
            </div>
          )}

          {sourceMode === 'link' && sourceType === 'VIDEO' && (
            <div className="rounded-md border border-(--ui-stroke-secondary) bg-background/40 p-4">
              <div className="mb-3 flex items-center gap-2">
                <Codicon className="text-primary" name="video" />
                <span className="text-sm font-medium">视频采集配置</span>
              </div>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <Field label="最高画质">
                  <ValueSelect onChange={setMaxHeight} value={maxHeight} values={['720', '1080', '1440', '2160']} />
                </Field>
                <Field label="字幕语言优先级">
                  <Input onChange={event => setSubtitleLanguages(event.target.value)} value={subtitleLanguages} />
                </Field>
              </div>
            </div>
          )}

          {sourceType === 'LIVE' && (
            <div className="rounded-md border border-(--ui-stroke-secondary) bg-background/40 p-4">
              <div className="mb-3 flex items-center gap-2">
                <Codicon className="text-primary" name="radio-tower" />
                <span className="text-sm font-medium">直播监控配置</span>
              </div>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
                <Field label="检测间隔（秒）">
                  <Input min="30" onChange={event => setPollInterval(event.target.value)} type="number" value={pollInterval} />
                </Field>
                <Field label="最长录制（分钟）">
                  <Input min="1" onChange={event => setMaxMinutes(event.target.value)} type="number" value={maxMinutes} />
                </Field>
                <Field label="录制画质">
                  <ValueSelect onChange={value => setQuality(value as typeof quality)} value={quality} values={['OD', 'UHD', 'HD', 'SD', 'LD']} />
                </Field>
              </div>
            </div>
          )}

          {sourceType && (
            <>
              <div className="rounded-md border border-(--ui-stroke-secondary) bg-background/40 p-4">
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <div className="text-sm font-medium">
                      {sourceType === 'LIVE'
                        ? '使用 faster-whisper 生成直播字幕'
                        : sourceMode === 'local'
                          ? '使用 faster-whisper 识别本地视频'
                          : '无字幕时使用 faster-whisper'}
                    </div>
                    <div className="mt-0.5 text-xs text-muted-foreground">公共 ASR 配置；任务支持分片检查点恢复。</div>
                  </div>
                  <Switch checked={asrEnabled} onCheckedChange={setAsrEnabled} />
                </div>

                {asrEnabled && (
                  <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
                    <Field label="faster-whisper 模型">
                      <Select onValueChange={setAsrModel} value={asrModel}>
                        <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          {MODELS.map(item => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}
                        </SelectContent>
                      </Select>
                    </Field>
                    <Field label="计算设备">
                      <ValueSelect onChange={value => setAsrDevice(value as typeof asrDevice)} value={asrDevice} values={['auto', 'cpu', 'cuda']} />
                    </Field>
                    <Field label="计算精度">
                      <ValueSelect onChange={setComputeType} value={computeType} values={['auto', 'int8', 'float16', 'float32']} />
                    </Field>
                    <Field label="语音语言（可选）">
                      <Input onChange={event => setLanguage(event.target.value)} placeholder="自动检测；或 zh、en、ja" value={language} />
                    </Field>
                    <Option checked={vadFilter} label="启用 VAD 静音过滤" onChange={setVadFilter} />
                    <Option checked={wordTimestamps} label="生成词级时间戳" onChange={setWordTimestamps} />
                  </div>
                )}
              </div>

              <div className="rounded-md border border-(--ui-stroke-secondary) bg-background/40 p-4">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 text-sm font-medium">
                      <Codicon className="text-primary" name="sparkle" />
                      Hermes 知识结果分析模型
                    </div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      {analysisSelection
                        ? `${analysisSelection.model} · ${analysisSelection.provider}`
                        : '使用 Hermes 全局模型'}
                    </div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      此选择只随当前内容任务传递，不会修改 Hermes 全局模型。
                    </div>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    {analysisSelection && (
                      <Button onClick={() => setAnalysisSelection(null)} type="button" variant="ghost">
                        使用全局模型
                      </Button>
                    )}
                    <DropdownMenu onOpenChange={setAnalysisModelPickerOpen} open={analysisModelPickerOpen}>
                      <DropdownMenuTrigger asChild>
                        <Button type="button" variant="secondary">
                          <Codicon name="settings-gear" />
                          选择模型
                          <Codicon name="chevron-down" size="0.7rem" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end" className="w-72 p-0">
                        <ModelMenuCloseContext.Provider value={() => setAnalysisModelPickerOpen(false)}>
                          <ModelCatalogMenu controller={analysisModelController} />
                        </ModelMenuCloseContext.Provider>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                </div>
              </div>

              <Option checked={autoAnalyze} label="Transcript 完成后自动使用 Hermes 生成知识" onChange={setAutoAnalyze} />
            </>
          )}
        </form>

        {(probe.error || pickLocalFile.error || pickCookiesFile.error || collect.error) && (
          <div className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
            {errorMessage(probe.error || pickLocalFile.error || pickCookiesFile.error || collect.error, '内容识别或任务创建失败')}
          </div>
        )}
      </section>

      <section className="min-h-72 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-5">
        {sourceMode === 'local' ? (
          !localPath.trim() ? (
            <div className="flex h-full min-h-64 flex-col items-center justify-center text-center text-muted-foreground">
              <Codicon className="mb-3 opacity-50" name="file-media" size="2rem" />
              <div className="text-sm">选择或填写一个本地视频路径</div>
              <div className="mt-1 text-xs">原文件只会被读取，不会被移动或删除。</div>
            </div>
          ) : (
            <div>
              <div className="flex aspect-video items-center justify-center rounded-md bg-black text-white/60">
                <Codicon name="file-media" size="3rem" />
              </div>
              <div className="mt-4 flex items-center gap-2">
                <Badge variant="outline">本地视频</Badge>
                <span className="text-[0.6875rem] font-semibold uppercase tracking-wider text-primary">LOCAL</span>
              </div>
              <h2 className="mt-2 text-base font-semibold leading-6">{localTitle.trim() || titleFromPath(localPath)}</h2>
              <p className="mt-1 text-xs text-muted-foreground">{localAuthor.trim() || '未知作者'}</p>
              <div className="mt-4 break-all rounded-md bg-(--ui-bg-quaternary) p-3 text-xs text-(--ui-text-secondary)">
                {localPath}
              </div>
              <div className="mt-3 rounded-md bg-(--ui-bg-quaternary) p-3 text-xs text-(--ui-text-secondary)">
                文件将复制到 Hermes 受控媒体目录，然后复用现有 ASR、Transcript 和知识分析流程。
              </div>
              <Button
                className="mt-4 w-full"
                disabled={!localReady || collect.isPending}
                onClick={() => collect.mutate()}
              >
                <Codicon name="workspace-trusted" />
                {collect.isPending ? '正在创建任务…' : '确认并开始识别'}
              </Button>
              {collect.data && (
                <div className="mt-3 rounded-md border border-emerald-500/30 bg-emerald-500/10 p-3 text-xs text-emerald-600 dark:text-emerald-300">
                  <strong>{collect.data.duplicate ? '该文件已存在，已复用任务' : '任务已创建'}</strong>
                  <div className="mt-1 break-all opacity-80">{collect.data.job.id}</div>
                </div>
              )}
            </div>
          )
        ) : !probe.data ? (
          <div className="flex h-full min-h-64 flex-col items-center justify-center text-center text-muted-foreground">
            <Codicon className="mb-3 opacity-50" name="preview" size="2rem" />
            <div className="text-sm">链接详情会显示在这里</div>
            <div className="mt-1 text-xs">识别不会下载完整视频或启动录制。</div>
          </div>
        ) : (
          <div>
            {probe.data.thumbnail_url && (
              <img
                alt="内容封面"
                className="aspect-video w-full rounded-md bg-black object-cover"
                onError={event => { event.currentTarget.hidden = true }}
                referrerPolicy="no-referrer"
                src={probe.data.thumbnail_url}
              />
            )}
            <div className="mt-4 flex items-center gap-2">
              <Badge variant="outline">{sourceType === 'LIVE' ? '直播间' : '普通视频'}</Badge>
              <span className="text-[0.6875rem] font-semibold uppercase tracking-wider text-primary">{probe.data.platform}</span>
              {sourceType === 'LIVE' && <Badge variant={probe.data.is_live ? 'default' : 'outline'}>{probe.data.is_live ? '正在直播' : '当前未开播'}</Badge>}
            </div>
            <h2 className="mt-2 text-base font-semibold leading-6">{probe.data.title}</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              {probe.data.author ?? '未知作者'}
              {sourceType === 'VIDEO' ? ` · ${durationLabel(probe.data.duration_seconds)}` : ''}
            </p>
            <div className="mt-4 rounded-md bg-(--ui-bg-quaternary) p-3 text-xs text-(--ui-text-secondary)">
              {sourceType === 'LIVE'
                ? probe.data.is_live
                  ? '确认后立即开始录制，并在下播后自动生成 Transcript 和 Hermes 知识。'
                  : '确认后创建订阅；Hermes 将在后台等待开播并自动录制。'
                : probe.data.subtitles.length > 0
                  ? `发现 ${probe.data.subtitles.length} 个字幕轨道：${probe.data.subtitles.slice(0, 6).map(item => item.language).join('、')}`
                  : '未发现字幕，将按左侧配置回退到 faster-whisper。'}
            </div>
            <Button className="mt-4 w-full" disabled={collect.isPending} onClick={() => collect.mutate()}>
              <Codicon name={sourceType === 'LIVE' ? 'radio-tower' : 'cloud-download'} />
              {collect.isPending ? '正在创建任务…' : sourceType === 'LIVE' ? '确认并开始监控' : '确认并开始采集'}
            </Button>
            {collect.data && (
              <div className="mt-3 rounded-md border border-emerald-500/30 bg-emerald-500/10 p-3 text-xs text-emerald-600 dark:text-emerald-300">
                <strong>{collect.data.duplicate ? '该来源已存在，已复用任务' : '任务已创建'}</strong>
                <div className="mt-1 break-all opacity-80">{collect.data.job.id}</div>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  )
}

function Field({ children, label }: { children: React.ReactNode; label: string }) {
  return (
    <label className="block space-y-1.5 text-xs font-medium">
      <span>{label}</span>
      {children}
    </label>
  )
}

function Option({ checked, label, onChange }: { checked: boolean; label: string; onChange: (value: boolean) => void }) {
  return (
    <label className="flex items-center gap-2 text-xs text-(--ui-text-secondary)">
      <Checkbox checked={checked} onCheckedChange={value => onChange(value === true)} />
      <span>{label}</span>
    </label>
  )
}

function ValueSelect({ onChange, value, values }: { onChange: (value: string) => void; value: string; values: string[] }) {
  return (
    <Select onValueChange={onChange} value={value}>
      <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
      <SelectContent>
        {values.map(item => <SelectItem key={item} value={item}>{item}</SelectItem>)}
      </SelectContent>
    </Select>
  )
}

function titleFromPath(path: string): string {
  const filename = path.trim().split(/[\\/]/).pop() ?? ''

  return filename.replace(/\.[^.]+$/, '')
}
