/**
 * Official Slack logo mark (2019), full color, served as a static asset
 * (/slack-logo.svg) — AutoSDE disallows inline SVGs and lucide-react v1
 * removed brand icons, so a static image is the compliant way to show a
 * brand mark. Scales via the `size` prop like lucide icons.
 */
export function SlackIcon({ size = 16 }: { size?: number }) {
  return <img src="/slack-logo.svg" width={size} height={size} alt="" aria-hidden="true" />
}
