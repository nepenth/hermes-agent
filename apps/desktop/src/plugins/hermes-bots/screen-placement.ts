import type { BotsText } from './i18n'

type PlacementText = Pick<BotsText['screen'], 'placementSandbox' | 'placementSshHost'>

// `display.status.placement` prefix → the badge naming where the screen runs. The gateway host gets none:
// the badge exists because a takeover there reaches another machine than the one running the gateway.
const PLACEMENT_BADGES: ReadonlyArray<readonly [string, (t: PlacementText, where: string) => string]> = [
  ['terminal:', (t, backend) => t.placementSandbox(backend)],
  ['ssh:', (t, desktopHost) => t.placementSshHost(desktopHost)]
]

export function placementBadge(t: PlacementText, placement: null | string | undefined): null | string {
  for (const [prefix, label] of PLACEMENT_BADGES) {
    if (placement?.startsWith(prefix)) {
      return label(t, placement.slice(prefix.length))
    }
  }

  return null
}
