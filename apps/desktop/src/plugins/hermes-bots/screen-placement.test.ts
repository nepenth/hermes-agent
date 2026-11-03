/**
 * Screen placement badge: the pane names where a takeover's input lands whenever that is not the gateway
 * host — the terminal backend's sandbox or a `bot_desktop.ssh` desktop host.
 */
import { describe, expect, it } from 'vitest'

import { placementBadge } from './screen-placement'

const t = {
  placementSandbox: (backend: string) => `sandbox ${backend}`,
  placementSshHost: (desktopHost: string) => `desktop host ${desktopHost}`
}

describe('placementBadge', () => {
  it('names the terminal sandbox and the SSH desktop host', () => {
    expect(placementBadge(t, 'terminal:docker')).toBe('sandbox docker')
    expect(placementBadge(t, 'ssh:agent@desktops.example:2222')).toBe('desktop host agent@desktops.example:2222')
  })

  it('shows nothing for the gateway host or a status without a placement', () => {
    expect(placementBadge(t, 'gateway')).toBeNull()
    expect(placementBadge(t, undefined)).toBeNull()
    expect(placementBadge(t, null)).toBeNull()
  })
})
