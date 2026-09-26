import http from 'node:http'
import type { AddressInfo } from 'node:net'

import { expect, test, vi } from 'vitest'

import { interruptedLocalRequestError, retryInterruptedLocalRead } from './backend-request-retry'

const reset = () => Object.assign(new Error('socket reset'), { code: 'ECONNRESET' })

test('a real socket reset is recovered through the replacement local port', async () => {
  const stopped = http.createServer((_request, response) => response.destroy())

  const ready = http.createServer((_request, response) => {
    response.setHeader('Content-Type', 'application/json')
    response.end('{"ready":true}')
  })

  await Promise.all([
    new Promise<void>(resolve => stopped.listen(0, '127.0.0.1', resolve)),
    new Promise<void>(resolve => ready.listen(0, '127.0.0.1', resolve))
  ])

  const original = { baseUrl: `http://127.0.0.1:${(stopped.address() as AddressInfo).port}` }
  const restarted = { baseUrl: `http://127.0.0.1:${(ready.address() as AddressInfo).port}` }

  const send = (connection: typeof original) =>
    new Promise<{ ready: boolean }>((resolve, reject) => {
      http
        .get(`${connection.baseUrl}/api/status`, response => {
          const chunks: Buffer[] = []

          response.on('data', chunk => chunks.push(chunk))
          response.on('end', () => resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))))
        })
        .on('error', reject)
    })

  try {
    await expect(retryInterruptedLocalRead({}, original, send, async () => restarted, async () => {})).resolves.toEqual({
      ready: true
    })
  } finally {
    stopped.close()
    ready.close()
  }
})

test('a local GET reconnects once after a backend restart and uses the new port', async () => {
  const original = { baseUrl: 'http://127.0.0.1:10001' }
  const restarted = { baseUrl: 'http://127.0.0.1:10002' }

  const send = vi.fn(async connection => {
    if (connection === original) {
      throw reset()
    }

    return { ok: true, port: connection.baseUrl }
  })

  const reconnect = vi.fn(async () => restarted)
  const pause = vi.fn(async () => {})

  await expect(retryInterruptedLocalRead({ method: 'GET' }, original, send, reconnect, pause)).resolves.toEqual({
    ok: true,
    port: restarted.baseUrl
  })
  expect(send).toHaveBeenCalledTimes(2)
  expect(reconnect).toHaveBeenCalledTimes(1)
  expect(pause).toHaveBeenCalledTimes(1)
})

test('a write is never replayed because the backend may have applied it', async () => {
  const error = reset()

  const send = vi.fn(async () => {
    throw error
  })

  const reconnect = vi.fn()

  await expect(
    retryInterruptedLocalRead({ method: 'POST', body: { value: 1 } }, { baseUrl: 'http://127.0.0.1:10001' }, send, reconnect)
  ).rejects.toBe(error)
  expect(send).toHaveBeenCalledTimes(1)
  expect(reconnect).not.toHaveBeenCalled()
  expect(interruptedLocalRequestError({ method: 'POST' }, error, 'http://127.0.0.1:10001')?.message).toContain(
    'may have completed'
  )
})

test('HTTP errors and remote resets are not treated as local restart failures', async () => {
  const httpError = new Error('500: failed')
  const reconnect = vi.fn()

  for (const [baseUrl, error] of [
    ['http://127.0.0.1:10001', httpError],
    ['https://gateway.example.com', reset()]
  ] as const) {
    await expect(retryInterruptedLocalRead({}, { baseUrl }, async () => Promise.reject(error), reconnect)).rejects.toBe(
      error
    )
    expect(interruptedLocalRequestError({}, error, baseUrl)).toBeNull()
  }

  expect(reconnect).not.toHaveBeenCalled()
})

test('a second reset is returned without an unbounded replay loop', async () => {
  const error = reset()

  const send = vi.fn(async () => {
    throw error
  })

  const reconnect = vi.fn(async () => ({ baseUrl: 'http://127.0.0.1:10002' }))

  await expect(
    retryInterruptedLocalRead({}, { baseUrl: 'http://127.0.0.1:10001' }, send, reconnect, async () => {})
  ).rejects.toBe(error)
  expect(send).toHaveBeenCalledTimes(2)
  expect(reconnect).toHaveBeenCalledTimes(1)
})
