import {
  Badge, Button, Checkbox, Codicon, Input, Loader, Select, SelectContent, SelectItem,
  SelectTrigger, SelectValue, Switch, useMutation, useQuery, useQueryClient
} from '@hermes/plugin-sdk'
import { useState } from 'react'

import { downloadAsrModel, fetchAsrStatus, fetchRuntimeStatus, updateAsrSettings } from './api'
import { errorMessage } from './format'
import type { AsrSettingsUpdate, AsrStatus } from './types'

const STATUS_KEY = ['video-knowledge', 'system', 'asr'] as const
const RUNTIME_KEY = ['video-knowledge', 'system', 'runtime'] as const

export function AsrSettingsView() {
  const status = useQuery({
    queryFn: fetchAsrStatus,
    queryKey: STATUS_KEY,
    refetchInterval: query => query.state.data?.models.some(model => model.downloading) ? 1500 : false,
    refetchOnMount: 'always'
  })

  if (status.isLoading) {return <Loader className="m-auto" />}

  if (status.isError) {
    return <div className="m-5 rounded border border-destructive/40 bg-destructive/10 p-4 text-xs text-destructive">{errorMessage(status.error, 'ASR 状态检测失败')}</div>
  }

  return <AsrSettingsForm initial={status.data!} />
}

function AsrSettingsForm({ initial }: { initial: AsrStatus }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<AsrSettingsUpdate>(() => toForm(initial))
  const [saved, setSaved] = useState(false)

  const save = useMutation({
    mutationFn: updateAsrSettings,
    onSuccess: value => {
      queryClient.setQueryData(STATUS_KEY, value)
      setForm(toForm(value))
      setSaved(true)
    }
  })

  const download = useMutation({
    mutationFn: downloadAsrModel,
    onMutate: modelName => {
      queryClient.setQueryData<AsrStatus>(STATUS_KEY, current => current
        ? {
            ...current,
            models: current.models.map(model => model.name === modelName ? { ...model, downloading: true } : model)
          }
        : current)
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: STATUS_KEY }),
    onSuccess: value => queryClient.setQueryData(STATUS_KEY, value)
  })

  const current = queryClient.getQueryData<AsrStatus>(STATUS_KEY) ?? initial
  const invalidChunk = form.overlap_seconds >= form.chunk_seconds

  const patch = <K extends keyof AsrSettingsUpdate>(key: K, value: AsrSettingsUpdate[K]) => {
    setSaved(false)
    setForm(previous => ({ ...previous, [key]: value }))
  }

  return (
    <div className="min-h-0 flex-1 overflow-auto p-5">
      <div className="mx-auto max-w-5xl space-y-4">
        <header>
          <div className="text-[0.6875rem] font-semibold uppercase tracking-wider text-primary">本地语音识别</div>
          <h2 className="mt-1 text-lg font-semibold">faster-whisper 配置与模型</h2>
          <p className="mt-1 text-xs text-muted-foreground">这里保存的是新任务默认值；“添加内容”仍可针对单个视频或直播覆盖这些配置。</p>
        </header>

        <RuntimeReadiness />

        <section className="rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-5">
          <div className="flex items-center justify-between gap-4">
            <div><h3 className="text-sm font-semibold">默认配置</h3><p className="mt-1 text-xs text-muted-foreground">保存后，之后创建的任务会自动使用这些值。</p></div>
            <div className="flex items-center gap-2">
              <Badge variant={form.enabled ? 'default' : 'outline'}>{form.enabled ? '已启用' : '已禁用'}</Badge>
              <Switch checked={form.enabled} onCheckedChange={value => patch('enabled', value)} />
            </div>
          </div>
          <div className="mt-5 grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            <Field label="默认模型"><ValueSelect onChange={value => patch('model', value)} value={form.model} values={current.models.map(model => model.name)} /></Field>
            <Field label="计算设备"><ValueSelect onChange={value => patch('configured_device', value as AsrSettingsUpdate['configured_device'])} value={form.configured_device} values={['auto', 'cpu', 'cuda']} /></Field>
            <Field label="计算精度"><ValueSelect onChange={value => patch('configured_compute_type', value as AsrSettingsUpdate['configured_compute_type'])} value={form.configured_compute_type} values={['auto', 'int8', 'float16', 'float32']} /></Field>
            <Field label="语言（可选）"><Input onChange={event => patch('language', event.target.value || null)} placeholder="自动检测；或 zh、en、ja" value={form.language ?? ''} /></Field>
            <Field label="长音频分片（秒）"><Input max="3600" min="30" onChange={event => patch('chunk_seconds', Number(event.target.value))} type="number" value={form.chunk_seconds} /></Field>
            <Field label="分片重叠（秒）"><Input max="30" min="0" onChange={event => patch('overlap_seconds', Number(event.target.value))} step="0.5" type="number" value={form.overlap_seconds} /></Field>
          </div>
          <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-3">
            <Option checked={form.vad_filter} label="启用 VAD 静音过滤" onChange={value => patch('vad_filter', value)} />
            <Option checked={form.word_timestamps} label="生成词级时间戳" onChange={value => patch('word_timestamps', value)} />
            <Option checked={form.auto_analyze} label="ASR 后自动生成知识结果" onChange={value => patch('auto_analyze', value)} />
          </div>
          <div className="mt-5 flex flex-wrap items-center gap-3">
            <Button disabled={save.isPending || invalidChunk} onClick={() => save.mutate(form)}><Codicon name="save" />{save.isPending ? '保存中…' : '保存默认配置'}</Button>
            {saved && <span className="text-xs text-emerald-600 dark:text-emerald-300">配置已保存</span>}
            {invalidChunk && <span className="text-xs text-destructive">重叠时长必须小于分片时长</span>}
            {save.error && <span className="text-xs text-destructive">{errorMessage(save.error, '保存失败')}</span>}
          </div>
        </section>

        <section className="rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div><h3 className="text-sm font-semibold">可选模型</h3><p className="mt-1 text-xs text-muted-foreground">状态来自本机 faster-whisper 模型缓存；未下载的模型可直接安装。</p></div>
            <div className="text-xs text-(--ui-text-secondary)">当前运行：{current.effective_device.toUpperCase()} / {current.effective_compute_type}<span className="ml-2">CUDA {current.cuda_available ? '可用' : '不可用'}</span></div>
          </div>
          <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {current.models.map(model => {
              const isDownloading = model.downloading || (download.isPending && download.variables === model.name)

              return (
                <div className={`rounded-md border p-4 ${model.name === form.model ? 'border-primary/50 bg-primary/5' : 'border-(--ui-stroke-secondary) bg-background/40'}`} key={model.name}>
                  <div className="flex items-center justify-between gap-2"><strong className="text-sm">{model.name}</strong><Badge variant={model.downloaded ? 'default' : 'outline'}>{model.downloaded ? '已下载' : isDownloading ? '下载中' : '未下载'}</Badge></div>
                  <div className="mt-1 text-[0.6875rem] text-muted-foreground">{model.size}</div>
                  <p className="mt-2 min-h-10 text-xs leading-5 text-(--ui-text-secondary)">{model.description}</p>
                  <div className="mt-3 flex gap-2">
                    <Button onClick={() => patch('model', model.name)} size="sm" variant="outline">设为默认</Button>
                    {!model.downloaded && <Button disabled={download.isPending || model.downloading} onClick={() => download.mutate(model.name)} size="sm"><Codicon name="cloud-download" />{isDownloading ? '下载中…' : '下载'}</Button>}
                  </div>
                </div>
              )
            })}
          </div>
          {download.error && <div className="mt-4 rounded border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">{errorMessage(download.error, '模型下载失败')}</div>}
        </section>
      </div>
    </div>
  )
}

function RuntimeReadiness() {
  const runtime = useQuery({
    queryFn: fetchRuntimeStatus,
    queryKey: RUNTIME_KEY,
    refetchOnMount: 'always',
    staleTime: 60_000
  })

  return (
    <section className="rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">运行环境检查</h3>
          <p className="mt-1 text-xs text-muted-foreground">安装包、媒体工具和转写运行时必须全部就绪。</p>
        </div>
        <Badge variant={runtime.data?.ready ? 'default' : 'outline'}>
          {runtime.isLoading ? '检查中' : runtime.data?.ready ? '全部就绪' : '需要处理'}
        </Badge>
      </div>
      {runtime.isError && <div className="mt-3 text-xs text-destructive">{errorMessage(runtime.error, '运行环境检查失败')}</div>}
      {runtime.data && (
        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
          {runtime.data.tools.map(tool => (
            <div className="rounded-md border border-(--ui-stroke-secondary) bg-background/40 p-3" key={tool.name}>
              <div className="flex items-center justify-between gap-2">
                <strong className="text-xs">{tool.name}</strong>
                <Badge variant={tool.available ? 'default' : 'outline'}>{tool.available ? '可用' : '缺失'}</Badge>
              </div>
              <div className="mt-2 truncate text-[0.6875rem] text-muted-foreground">
                {tool.version ?? tool.detail ?? '版本未知'}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function toForm(value: AsrStatus): AsrSettingsUpdate {
  return {
    enabled: value.enabled, model: value.model,
    configured_device: value.configured_device as AsrSettingsUpdate['configured_device'],
    configured_compute_type: value.configured_compute_type as AsrSettingsUpdate['configured_compute_type'],
    language: value.language, vad_filter: value.vad_filter, word_timestamps: value.word_timestamps,
    chunk_seconds: value.chunk_seconds, overlap_seconds: value.overlap_seconds, auto_analyze: value.auto_analyze
  }
}

function Field({ children, label }: { children: React.ReactNode; label: string }) {
  return <label className="block space-y-1.5 text-xs font-medium"><span>{label}</span>{children}</label>
}

function Option({ checked, label, onChange }: { checked: boolean; label: string; onChange: (value: boolean) => void }) {
  return <label className="flex items-center gap-2 text-xs text-(--ui-text-secondary)"><Checkbox checked={checked} onCheckedChange={value => onChange(value === true)} /><span>{label}</span></label>
}

function ValueSelect({ onChange, value, values }: { onChange: (value: string) => void; value: string; values: string[] }) {
  return <Select onValueChange={onChange} value={value}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent>{values.map(item => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectContent></Select>
}
