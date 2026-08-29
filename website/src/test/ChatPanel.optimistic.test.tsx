// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks —
// use the same lightweight mock the SettingsSelect unit tests use so options
// are real role="option" nodes and the trigger renders the selected option's
// label (the contract these optimistic-value assertions depend on).
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { patchConfigMock, kirocrewConfigMock, modelsMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() =>
    Promise.resolve({ agent: { model: 'auto', reasoning_effort: '' } })
  ),
  modelsMock: vi.fn(() =>
    Promise.resolve([
      { model_name: 'auto', description: 'Default' },
      { model_name: 'claude-opus-4.8', description: 'Opus' },
      { model_name: 'claude-haiku-4.5', description: 'Haiku' },
    ])
  ),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' }),
    kirocrewConfig: kirocrewConfigMock,
    models: modelsMock,
    patchConfig: patchConfigMock,
    updateDashboardConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const seed = (agent: Record<string, unknown>) =>
  kirocrewConfigMock.mockImplementation(() => Promise.resolve({ agent }) as never)

/** A promise whose resolution the test controls, so trigger state can be
 *  asserted while the PATCH is still in flight (before the optimistic write
 *  is ever reconciled or rolled back). */
function deferred<T = unknown>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

/** Open a SettingsSelect by label and return its option nodes.
 *  Waits for the control to leave its loading-disabled state first — the
 *  trigger exists (inert) while the config query is still in flight. */
async function openSelect(label: string) {
  const trigger = await screen.findByRole('combobox', { name: label })
  await waitFor(() => expect(trigger).not.toHaveAttribute('data-disabled'))
  fireEvent.click(trigger)
  return screen.getAllByRole('option')
}

describe('ChatPanel — optimistic model/effort selectors', () => {
  beforeEach(() => {
    patchConfigMock.mockClear()
    modelsMock.mockClear()
    seed({ model: 'claude-opus-4.8', reasoning_effort: 'high' })
  })

  describe('immediate optimistic value (before PATCH resolves)', () => {
    it('shows the picked default model in the trigger before the PATCH settles', async () => {
      // A deferred PATCH lets us assert the trigger updated purely from the
      // optimistic cache write, with no server round-trip.
      const d = deferred()
      patchConfigMock.mockImplementationOnce(() => d.promise as never)
      seed({ model: 'auto', reasoning_effort: '' })
      wrap(<ChatPanel />)
      await waitFor(() => expect(modelsMock).toHaveBeenCalled())

      await openSelect('Default Model')
      fireEvent.click(screen.getByRole('option', { name: 'claude-opus-4.8' }))

      // No await on the PATCH: the trigger must reflect the pick immediately.
      await waitFor(() =>
        expect(screen.getByRole('combobox', { name: 'Default Model' })).toHaveTextContent(
          'claude-opus-4.8'
        )
      )
      expect(patchConfigMock).toHaveBeenCalledWith('agent.model', 'claude-opus-4.8')
      d.resolve({})
    })

    it('shows the picked default reasoning effort in the trigger before the PATCH settles', async () => {
      const d = deferred()
      patchConfigMock.mockImplementationOnce(() => d.promise as never)
      seed({ model: 'claude-opus-4.8', reasoning_effort: '' })
      wrap(<ChatPanel />)

      await openSelect('Default Reasoning Effort')
      fireEvent.click(screen.getByRole('option', { name: 'High' }))

      await waitFor(() =>
        expect(
          screen.getByRole('combobox', { name: 'Default Reasoning Effort' })
        ).toHaveTextContent('High')
      )
      expect(patchConfigMock).toHaveBeenCalledWith('agent.reasoning_effort', 'high')
      d.resolve({})
    })

    it('shows the picked background (role) model in the trigger before the PATCH settles', async () => {
      const d = deferred()
      patchConfigMock.mockImplementationOnce(() => d.promise as never)
      seed({ model: 'claude-opus-4.8', role_models: { background: 'auto' } })
      wrap(<ChatPanel />)
      await waitFor(() => expect(modelsMock).toHaveBeenCalled())

      await openSelect('Background Model')
      fireEvent.click(screen.getByRole('option', { name: 'claude-haiku-4.5' }))

      await waitFor(() =>
        expect(screen.getByRole('combobox', { name: 'Background Model' })).toHaveTextContent(
          'claude-haiku-4.5'
        )
      )
      expect(patchConfigMock).toHaveBeenCalledWith('agent.role_models.background', 'claude-haiku-4.5')
      d.resolve({})
    })

    it('shows the picked subagent (role) model in the trigger before the PATCH settles', async () => {
      const d = deferred()
      patchConfigMock.mockImplementationOnce(() => d.promise as never)
      seed({ model: 'claude-opus-4.8', role_models: { subagent: 'auto' } })
      wrap(<ChatPanel />)
      await waitFor(() => expect(modelsMock).toHaveBeenCalled())

      await openSelect('Subagent Model')
      fireEvent.click(screen.getByRole('option', { name: 'claude-haiku-4.5' }))

      await waitFor(() =>
        expect(screen.getByRole('combobox', { name: 'Subagent Model' })).toHaveTextContent(
          'claude-haiku-4.5'
        )
      )
      // Distinct path from the background role model: a copy-paste of the
      // background field name would surface here.
      expect(patchConfigMock).toHaveBeenCalledWith('agent.role_models.subagent', 'claude-haiku-4.5')
      d.resolve({})
    })

    it('shows the picked background effort in the trigger before the PATCH settles', async () => {
      const d = deferred()
      patchConfigMock.mockImplementationOnce(() => d.promise as never)
      // Default model is reasoning-capable and the role model stays on auto, so
      // bgEffortSupported resolves via defaultModel and the row is enabled.
      // (subEffortSupported resolves the same way for the subagent case.)
      seed({ model: 'claude-opus-4.8', role_efforts: { background: '' } })
      wrap(<ChatPanel />)

      await openSelect('Background Effort')
      fireEvent.click(screen.getByRole('option', { name: 'High' }))

      await waitFor(() =>
        expect(screen.getByRole('combobox', { name: 'Background Effort' })).toHaveTextContent(
          'High'
        )
      )
      expect(patchConfigMock).toHaveBeenCalledWith('agent.role_efforts.background', 'high')
      d.resolve({})
    })

    it('shows the picked subagent effort in the trigger before the PATCH settles', async () => {
      const d = deferred()
      patchConfigMock.mockImplementationOnce(() => d.promise as never)
      seed({ model: 'claude-opus-4.8', role_efforts: { subagent: '' } })
      wrap(<ChatPanel />)

      await openSelect('Subagent Effort')
      fireEvent.click(screen.getByRole('option', { name: 'High' }))

      await waitFor(() =>
        expect(screen.getByRole('combobox', { name: 'Subagent Effort' })).toHaveTextContent(
          'High'
        )
      )
      // Distinct path from the background effort: the field-name difference is
      // exactly what this assertion guards.
      expect(patchConfigMock).toHaveBeenCalledWith('agent.role_efforts.subagent', 'high')
      d.resolve({})
    })

    it('shows the picked fallback model in the trigger before the PATCH settles', async () => {
      const d = deferred()
      patchConfigMock.mockImplementationOnce(() => d.promise as never)
      seed({ model: 'claude-opus-4.8', fallback_model: 'auto' })
      wrap(<ChatPanel />)
      await waitFor(() => expect(modelsMock).toHaveBeenCalled())

      await openSelect('Fallback Model')
      fireEvent.click(screen.getByRole('option', { name: 'claude-opus-4.8' }))

      await waitFor(() =>
        expect(screen.getByRole('combobox', { name: 'Fallback Model' })).toHaveTextContent(
          'claude-opus-4.8'
        )
      )
      expect(patchConfigMock).toHaveBeenCalledWith('agent.fallback_model', 'claude-opus-4.8')
      d.resolve({})
    })
  })

  describe('rollback + error banner on failure', () => {
    it('reverts the default model and shows the failure banner when the PATCH rejects', async () => {
      seed({ model: 'auto', reasoning_effort: '' })
      // Keep the server value at 'auto' so the reconcile refetch cannot mask a
      // missing rollback — the revert must come from onError's setQueryData.
      patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')) as never)
      wrap(<ChatPanel />)
      await waitFor(() => expect(modelsMock).toHaveBeenCalled())

      await openSelect('Default Model')
      fireEvent.click(screen.getByRole('option', { name: 'claude-opus-4.8' }))

      expect(await screen.findByText(/Failed to save default model/)).toBeInTheDocument()
      await waitFor(() =>
        expect(screen.getByRole('combobox', { name: 'Default Model' })).toHaveTextContent(
          'Default (auto)'
        )
      )
    })

    it('reverts the default reasoning effort and shows the failure banner when the PATCH rejects', async () => {
      seed({ model: 'claude-opus-4.8', reasoning_effort: '' })
      patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')) as never)
      wrap(<ChatPanel />)

      await openSelect('Default Reasoning Effort')
      fireEvent.click(screen.getByRole('option', { name: 'High' }))

      expect(
        await screen.findByText(/Failed to save default reasoning effort/)
      ).toBeInTheDocument()
      await waitFor(() =>
        expect(
          screen.getByRole('combobox', { name: 'Default Reasoning Effort' })
        ).toHaveTextContent('Model default')
      )
    })

    it('reverts the background role model and shows the failure banner when the PATCH rejects', async () => {
      seed({ model: 'claude-opus-4.8', role_models: { background: 'auto' } })
      patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')) as never)
      wrap(<ChatPanel />)
      await waitFor(() => expect(modelsMock).toHaveBeenCalled())

      await openSelect('Background Model')
      fireEvent.click(screen.getByRole('option', { name: 'claude-haiku-4.5' }))

      expect(await screen.findByText(/Failed to save/)).toBeInTheDocument()
      await waitFor(() =>
        expect(screen.getByRole('combobox', { name: 'Background Model' })).toHaveTextContent(
          'Auto'
        )
      )
    })

    it('reverts the subagent role model and shows the failure banner when the PATCH rejects', async () => {
      seed({ model: 'claude-opus-4.8', role_models: { subagent: 'auto' } })
      patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')) as never)
      wrap(<ChatPanel />)
      await waitFor(() => expect(modelsMock).toHaveBeenCalled())

      await openSelect('Subagent Model')
      fireEvent.click(screen.getByRole('option', { name: 'claude-haiku-4.5' }))

      expect(await screen.findByText(/Failed to save/)).toBeInTheDocument()
      await waitFor(() =>
        expect(screen.getByRole('combobox', { name: 'Subagent Model' })).toHaveTextContent(
          'Auto'
        )
      )
    })

    it('reverts the background effort and shows the failure banner when the PATCH rejects', async () => {
      seed({ model: 'claude-opus-4.8', role_efforts: { background: '' } })
      patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')) as never)
      wrap(<ChatPanel />)

      await openSelect('Background Effort')
      fireEvent.click(screen.getByRole('option', { name: 'High' }))

      expect(await screen.findByText(/Failed to save/)).toBeInTheDocument()
      await waitFor(() =>
        expect(screen.getByRole('combobox', { name: 'Background Effort' })).toHaveTextContent(
          'Model default'
        )
      )
    })

    it('reverts the subagent effort and shows the failure banner when the PATCH rejects', async () => {
      seed({ model: 'claude-opus-4.8', role_efforts: { subagent: '' } })
      patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')) as never)
      wrap(<ChatPanel />)

      await openSelect('Subagent Effort')
      fireEvent.click(screen.getByRole('option', { name: 'High' }))

      expect(await screen.findByText(/Failed to save/)).toBeInTheDocument()
      await waitFor(() =>
        expect(screen.getByRole('combobox', { name: 'Subagent Effort' })).toHaveTextContent(
          'Model default'
        )
      )
    })

    it('reverts the fallback model and appends the backend deny reason to the banner', async () => {
      seed({ model: 'claude-opus-4.8', fallback_model: 'auto' })
      patchConfigMock.mockImplementationOnce(
        () => Promise.reject(new Error('model not entitled')) as never
      )
      wrap(<ChatPanel />)
      await waitFor(() => expect(modelsMock).toHaveBeenCalled())

      await openSelect('Fallback Model')
      fireEvent.click(screen.getByRole('option', { name: 'claude-opus-4.8' }))

      // The generic failure line plus the appended ": <reason>" suffix.
      expect(
        await screen.findByText(/Failed to save fallback model.*model not entitled/)
      ).toBeInTheDocument()
      await waitFor(() =>
        expect(screen.getByRole('combobox', { name: 'Fallback Model' })).toHaveTextContent(
          'Auto'
        )
      )
    })
  })

  describe('reconcile refetch on settle (success and error)', () => {
    it('refetches kirocrewConfig after a successful PATCH', async () => {
      seed({ model: 'auto', reasoning_effort: '' })
      wrap(<ChatPanel />)
      await waitFor(() => expect(modelsMock).toHaveBeenCalled())
      const callsBefore = kirocrewConfigMock.mock.calls.length

      await openSelect('Default Model')
      fireEvent.click(screen.getByRole('option', { name: 'claude-opus-4.8' }))

      await waitFor(() =>
        expect(patchConfigMock).toHaveBeenCalledWith('agent.model', 'claude-opus-4.8')
      )
      // onSettled invalidation triggers a fresh config fetch.
      await waitFor(() =>
        expect(kirocrewConfigMock.mock.calls.length).toBeGreaterThan(callsBefore)
      )
    })

    it('refetches kirocrewConfig after a failed PATCH (invalidate on error too)', async () => {
      seed({ model: 'auto', reasoning_effort: '' })
      patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')) as never)
      wrap(<ChatPanel />)
      await waitFor(() => expect(modelsMock).toHaveBeenCalled())
      const callsBefore = kirocrewConfigMock.mock.calls.length

      await openSelect('Default Model')
      fireEvent.click(screen.getByRole('option', { name: 'claude-opus-4.8' }))

      expect(await screen.findByText(/Failed to save default model/)).toBeInTheDocument()
      await waitFor(() =>
        expect(kirocrewConfigMock.mock.calls.length).toBeGreaterThan(callsBefore)
      )
    })
  })
})
