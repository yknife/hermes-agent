import {
  Badge,
  Button,
  Checkbox,
  Codicon,
  Input,
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

import { createLiveSource, fetchAsrStatus, ingest, probeSource } from './api'
import { durationLabel, errorMessage } from './format'
import type { IngestOptions, LiveSourceOptions } from './types'

const MODELS = [
  { label: 'tiny（最快，低资源）', value: 'tiny' },
  { label: 'base（快速）', value: 'base' },
  { label: 'small（推荐）', value: 'small' },
  { label: 'medium（更准确）', value: 'medium' },
  { label: 'large-v3（最高精度，高配置）', value: 'large-v3' },
  { label: 'large-v3-turbo（更快、更省资源）', value: 'large-v3-turbo' }
]

export function AddContentView({ onCreated }: { onCreated: (jobId: string) => void }) {
  const queryClient = useQueryClient()
  const [url, setUrl] = useState('')
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
  const [defaultsApplied, setDefaultsApplied] = useState(false)
  const asrStatus = useQuery({ queryFn: fetchAsrStatus, queryKey: ['video-knowledge', 'system', 'asr'] })
  const probe = useMutation({ mutationFn: probeSource })

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
      if (!probe.data) {
        throw new Error('请先识别链接')
      }

      const asrOptions = {
        asr_compute_type: computeType,
        asr_device: asrDevice,
        asr_enabled: asrEnabled,
        asr_language: language.trim() || null,
        asr_model: asrModel,
        asr_vad_filter: vadFilter,
        asr_word_timestamps: wordTimestamps
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

  const runProbe = () => {
      if (!url.trim()) {
        return
      }

      collect.reset()
      probe.mutate(url.trim())
  }

  const sourceType = probe.data?.source_type

  return (
    <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-auto p-5 xl:grid-cols-[minmax(22rem,1fr)_minmax(20rem,0.9fr)]">
      <section className="rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-5">
        <div className="mb-5">
          <div className="text-[0.6875rem] font-semibold uppercase tracking-wider text-primary">统一内容入口</div>
          <h2 className="mt-1 text-base font-semibold">添加视频或直播链接</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            Hermes 自动识别链接类型，并显示视频采集或直播监控所需的配置。
          </p>
        </div>

        <form
          className="space-y-5"
          onSubmit={event => {
            event.preventDefault()
            runProbe()
          }}
        >
          <Field label="内容链接">
            <div className="flex gap-2">
              <Input
                aria-label="内容链接"
                onChange={event => {
                  setUrl(event.target.value)
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

          {sourceType === 'VIDEO' && (
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
                      {sourceType === 'LIVE' ? '使用 faster-whisper 生成直播字幕' : '无字幕时使用 faster-whisper'}
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

              <Option checked={autoAnalyze} label="Transcript 完成后自动使用 Hermes 生成知识" onChange={setAutoAnalyze} />
            </>
          )}
        </form>

        {(probe.error || collect.error) && (
          <div className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
            {errorMessage(probe.error || collect.error, '链接识别或任务创建失败')}
          </div>
        )}
      </section>

      <section className="min-h-72 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-5">
        {!probe.data ? (
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
