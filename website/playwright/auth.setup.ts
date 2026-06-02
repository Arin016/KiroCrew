import { test as setup } from '@playwright/test'

/**
 * Auth setup: exchanges PLAYWRIGHT_TOKEN for a session cookie once, then
 * persists storage state to .auth/state.json. All test projects reuse the
 * saved state so tokens never appear in test-level traces or videos.
 */

const STATE_PATH = 'playwright/.auth/state.json'

setup('authenticate', async ({ page }) => {
  const token = process.env.PLAYWRIGHT_TOKEN
  if (!token) {
    // Unauthenticated gateway. Still persist an empty storage state so the
    // `storageState` path in playwright.config.ts always resolves — otherwise
    // every test fails with ENOENT when running without a token.
    await page.context().storageState({ path: STATE_PATH })
    return
  }
  await page.goto(`/?token=${encodeURIComponent(token)}`, { waitUntil: 'domcontentloaded', timeout: 30000 })
  await page.waitForLoadState('load', { timeout: 30000 })
  await page.context().storageState({ path: STATE_PATH })
})
