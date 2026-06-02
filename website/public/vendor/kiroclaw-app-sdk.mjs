// Vendor stub: re-exports @kiroclaw/app-sdk from the host.
const m = window.__kiroclaw_modules?.['@kiroclaw/app-sdk']
if (!m) throw new Error('[vendor/kiroclaw-app-sdk] Host modules not initialized.')
export const {
  useAppApi, useAppEvents, useTheme, useAppInfo, useNavigate, useNotify,
  useNavBadge, useChatLauncher, AppApiProvider,
} = m
