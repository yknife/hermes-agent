import {
  Badge, Button, Checkbox, Codicon, host, Input, Loader, Select, SelectContent, SelectItem,
  SelectTrigger, SelectValue, Switch, useMutation, useQuery, useQueryClient
} from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'

import {
  downloadAsrModel,
  fetchAsrStatus,
  fetchRuntimeStatus,
  fetchStorageSettings,
  migrateStorage,
  updateAsrSettings
} from './api'
import { errorMessage, fileSize } from './format'
import type { AsrSettingsUpdate, AsrStatus, StorageMigrationPhase, StorageSettings } from './types'

const STATUS_KEY = ['video-knowledge', 'system', 'asr'] as const
const RUNTIME_KEY = ['video-knowledge', 'system', 'runtime'] as const
const STORAGE_KEY = ['video-knowledge', 'system', 'storage'] as const
const ACTIVE_MIGRATION_PHASES = new Set<StorageMigrationPhase>(['COPYING', 'VERIFYING', 'SWITCHING', 'CLEANING'])

export function SystemSettingsView() {
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
          <div className="text-[0.6875rem] font-semibold uppercase tracking-wider text-primary">Video Knowledge</div>
          <h2 className="mt-1 text-lg font-semibold">系统设置</h2>
          <p className="mt-1 text-xs text-muted-foreground">管理媒体资产存储目录、运行环境和 faster-whisper 默认配置。</p>
        </header>

        <StorageSettingsSection />

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

function StorageSettingsSection() {
  const queryClient = useQueryClient()
  const [targetPath, setTargetPath] = useState('')

  const storage = useQuery({
    queryFn: fetchStorageSettings,
    queryKey: STORAGE_KEY,
    refetchInterval: query => ACTIVE_MIGRATION_PHASES.has(query.state.data?.migration.phase ?? 'IDLE') ? 500 : false,
    refetchOnMount: 'always'
  })

  const migration = storage.data?.migration
  const active = ACTIVE_MIGRATION_PHASES.has(migration?.phase ?? 'IDLE')

  useEffect(() => {
    if (storage.data && !targetPath) {
      setTargetPath(storage.data.storage_root)
    }
  }, [storage.data, targetPath])

  const picker = useMutation({
    mutationFn: () => host.selectPaths({
      defaultPath: targetPath || storage.data?.storage_root,
      directories: true,
      multiple: false,
      title: '选择新的媒体资产存储目录（必须为空）'
    }),
    onSuccess: paths => {
      if (paths[0]) {
        setTargetPath(paths[0])
      }
    }
  })

  const migrate = useMutation({
    mutationFn: migrateStorage,
    onSuccess: value => {
      queryClient.setQueryData<StorageSettings>(STORAGE_KEY, value)
      void queryClient.invalidateQueries({ queryKey: ['video-knowledge'] })
    }
  })

  useEffect(() => {
    if (migration?.phase === 'COMPLETED' && storage.data) {
      setTargetPath(storage.data.storage_root)
      void queryClient.invalidateQueries({ queryKey: ['video-knowledge'] })
    }
  }, [migration?.phase, queryClient, storage.data])

  const changed = Boolean(targetPath.trim() && targetPath.trim() !== storage.data?.storage_root)
  const phaseLabel = migration ? storagePhaseLabel(migration.phase) : '读取中'

  return (
    <section className="rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">媒体资产存储目录</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            视频、封面、ASR 音频和 Transcript 文件保存在这里；数据库仍保留在 Hermes 配置目录。
          </p>
        </div>
        <Badge variant={active ? 'default' : 'outline'}>{active ? phaseLabel : '全局配置'}</Badge>
      </div>

      {storage.isLoading ? (
        <Loader className="mt-5" />
      ) : storage.isError ? (
        <div className="mt-4 text-xs text-destructive">{errorMessage(storage.error, '读取存储设置失败')}</div>
      ) : (
        <>
          <div className="mt-5 flex gap-2">
            <Input
              aria-label="媒体资产存储目录"
              disabled={active}
              onChange={event => setTargetPath(event.target.value)}
              placeholder="例如 D:\\HermesMedia"
              value={targetPath}
            />
            <Button disabled={active || picker.isPending} onClick={() => picker.mutate()} type="button" variant="secondary">
              <Codicon name="folder-opened" />
              {picker.isPending ? '选择中…' : '选择目录'}
            </Button>
            <Button
              disabled={!changed || active || migrate.isPending}
              onClick={() => migrate.mutate(targetPath.trim())}
              type="button"
            >
              <Codicon name="move" />
              {migrate.isPending ? '准备迁移…' : '迁移并应用'}
            </Button>
          </div>
          <div className="mt-2 break-all text-[0.6875rem] text-muted-foreground">
            当前目录：{storage.data?.storage_root}
          </div>
          <p className="mt-2 text-[0.6875rem] text-muted-foreground">
            目标文件夹必须为空。迁移期间暂停创建任务；复制并逐文件哈希校验成功后才切换目录，最后清理旧目录。
          </p>
        </>
      )}

      {migration && migration.phase !== 'IDLE' && (
        <div className="mt-4 rounded-md border border-(--ui-stroke-secondary) bg-background/40 p-4">
          <div className="flex items-center justify-between gap-3 text-xs">
            <strong>{phaseLabel}</strong>
            <span>{migration.progress.toFixed(1)}%</span>
          </div>
          <div
            aria-valuemax={100}
            aria-valuemin={0}
            aria-valuenow={migration.progress}
            className="mt-3 h-2 overflow-hidden rounded-full bg-muted"
            role="progressbar"
          >
            <div
              className={`h-full rounded-full transition-[width] duration-300 ${migration.phase === 'FAILED' ? 'bg-destructive' : 'bg-primary'}`}
              style={{ width: `${migration.progress}%` }}
            />
          </div>
          <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[0.6875rem] text-muted-foreground">
            <span>{migration.processed_files}/{migration.total_files} 个文件步骤</span>
            <span>{fileSize(migration.processed_bytes)} / {fileSize(migration.total_bytes)}</span>
            {migration.target_path && <span className="break-all">目标：{migration.target_path}</span>}
          </div>
          {migration.error && <div className="mt-2 text-xs text-destructive">{migration.error}</div>}
          {migration.warning && <div className="mt-2 text-xs text-amber-600 dark:text-amber-300">{migration.warning}</div>}
        </div>
      )}
      {migrate.error && <div className="mt-3 text-xs text-destructive">{errorMessage(migrate.error, '启动迁移失败')}</div>}
      {picker.error && <div className="mt-3 text-xs text-destructive">{errorMessage(picker.error, '选择目录失败')}</div>}
    </section>
  )
}

function storagePhaseLabel(phase: StorageMigrationPhase): string {
  return {
    IDLE: '尚未迁移',
    COPYING: '正在复制媒体资产',
    VERIFYING: '正在校验迁移数据',
    SWITCHING: '正在切换存储目录',
    CLEANING: '正在清理旧目录',
    COMPLETED: '迁移完成',
    FAILED: '迁移失败'
  }[phase]
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
