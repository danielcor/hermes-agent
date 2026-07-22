import { beforeEach, describe, expect, it } from 'vitest'

import { clearSpawnHistory, getSpawnHistory, pushDiskSnapshot } from '../app/spawnHistoryStore.js'

describe('spawnHistoryStore status normalization', () => {
  beforeEach(() => {
    clearSpawnHistory()
  })

  it('keeps timeout/error statuses from disk snapshots', () => {
    pushDiskSnapshot(
      {
        finished_at: 1_700_000_001,
        label: 'status test',
        session_id: 'sess-1',
        started_at: 1_700_000_000,
        subagents: [
          { goal: 'timeout child', id: 'sa-timeout', index: 0, status: 'timeout' },
          { goal: 'error child', id: 'sa-error', index: 1, status: 'error' }
        ]
      },
      '/tmp/snap-timeout-error.json'
    )

    const statuses = getSpawnHistory()[0]?.subagents.map(s => s.status)

    expect(statuses).toEqual(['timeout', 'error'])
  })

  it('falls back unknown disk statuses to completed', () => {
    pushDiskSnapshot(
      {
        finished_at: 1_700_000_011,
        label: 'unknown status test',
        session_id: 'sess-2',
        started_at: 1_700_000_010,
        subagents: [{ goal: 'mystery child', id: 'sa-unknown', index: 0, status: 'mystery_status' }]
      },
      '/tmp/snap-unknown.json'
    )

    const status = getSpawnHistory()[0]?.subagents[0]?.status

    expect(status).toBe('completed')
  })

  it('restores delegation route identity from disk snapshots', () => {
    pushDiskSnapshot(
      {
        finished_at: 1_700_000_021,
        label: 'route test',
        session_id: 'sess-3',
        started_at: 1_700_000_020,
        subagents: [{
          goal: 'review', id: 'sa-route', index: 0, lane: 'review',
          model: 'grok-4.5', provider: 'xai-oauth', status: 'completed'
        }]
      },
      '/tmp/snap-route.json'
    )

    expect(getSpawnHistory()[0]?.subagents[0]).toMatchObject({
      lane: 'review', model: 'grok-4.5', provider: 'xai-oauth'
    })
  })

  it('restores qualified completion exit reasons from disk snapshots', () => {
    pushDiskSnapshot(
      {
        finished_at: 1_700_000_031,
        label: 'exit reason test',
        session_id: 'sess-4',
        started_at: 1_700_000_030,
        subagents: [{
          exitReason: 'max_iterations', goal: 'review', id: 'sa-exit', index: 0,
          status: 'completed'
        }]
      },
      '/tmp/snap-exit-reason.json'
    )

    expect(getSpawnHistory()[0]?.subagents[0]?.exitReason).toBe('max_iterations')
  })
})
