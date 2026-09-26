/** A backend restart may sever an in-flight IPC REST request. */

type ApiRequest = { body?: unknown; method?: string; upload?: unknown }
type BackendConnection = { baseUrl: string }

function methodOf(request: ApiRequest | null | undefined): string {
  return String(request?.method || 'GET').toUpperCase()
}

function transportCode(error: unknown): string | null {
  if (!error || typeof error !== 'object') {
    return null
  }

  const candidate = error as { cause?: unknown; code?: unknown }

  if (typeof candidate.code === 'string') {
    return candidate.code
  }

  return candidate.cause ? transportCode(candidate.cause) : null
}

function localBackendTransportError(error: unknown, baseUrl: string): boolean {
  let hostname: string

  try {
    hostname = new URL(baseUrl).hostname
  } catch {
    return false
  }

  return (
    ['127.0.0.1', 'localhost', '[::1]'].includes(hostname) &&
    ['ECONNRESET', 'ECONNREFUSED', 'EPIPE'].includes(transportCode(error) || '')
  )
}

function retryableLocalRead(request: ApiRequest | null | undefined, error: unknown, baseUrl: string): boolean {
  return (
    methodOf(request) === 'GET' &&
    request?.body === undefined &&
    request?.upload === undefined &&
    localBackendTransportError(error, baseUrl)
  )
}

/** Retry once after the managed backend has had time to publish its new port. */
async function retryInterruptedLocalRead<TConnection extends BackendConnection, TResult>(
  request: ApiRequest | null | undefined,
  connection: TConnection,
  send: (connection: TConnection) => Promise<TResult>,
  reconnect: () => Promise<TConnection>,
  pause: () => Promise<void> = () => new Promise(resolve => setTimeout(resolve, 750))
): Promise<TResult> {
  try {
    return await send(connection)
  } catch (error) {
    if (!retryableLocalRead(request, error, connection.baseUrl)) {
      throw error
    }

    await pause()

    return send(await reconnect())
  }
}

function interruptedLocalRequestError(
  request: ApiRequest | null | undefined,
  error: unknown,
  baseUrl: string | null
): Error | null {
  if (!baseUrl || !localBackendTransportError(error, baseUrl)) {
    return null
  }

  const message =
    methodOf(request) === 'GET'
      ? 'Hermes backend disconnected during this read. Please retry when it is ready.'
      : 'Hermes backend disconnected during this request. It may have completed; check its status before retrying.'

  return new Error(message, { cause: error })
}

export { interruptedLocalRequestError, retryInterruptedLocalRead }
