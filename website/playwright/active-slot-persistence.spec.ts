import { test, expect } from '@playwright/test'

test.describe('Active slot persistence across mode switches', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/chat', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.session-row').first()).toBeVisible({ timeout: 10000 })
  })

  test('remembers selected session when switching Chat → Autopilot → Chat', async ({ page }) => {
    const rows = page.locator('.session-row')
    const count = await rows.count()
    if (count < 2) test.skip(true, 'Need at least 2 chat sessions')

    // Click the second session (not the first, which is the default fallback)
    await rows.nth(1).click()
    await expect(rows.nth(1)).toHaveClass(/session-active/, { timeout: 2000 })

    // Grab the title of the selected session
    const selectedTitle = await rows.nth(1).locator('.font-mono').textContent()

    // Switch to Autopilot
    await page.locator('aside').getByText('Autopilot').click()
    await page.waitForURL('**/orchestrated**')

    // Switch back to Chat
    await page.locator('aside').getByText('Chat').click()
    await page.waitForURL('**/chat**')
    await expect(page.locator('.session-row').first()).toBeVisible({ timeout: 10000 })

    // The previously selected session should still be active (not fallen back to first)
    const activeRow = page.locator('.session-row.session-active')
    await expect(activeRow).toBeVisible({ timeout: 5000 })
    const restoredTitle = await activeRow.locator('.font-mono').textContent()
    expect(restoredTitle).toBe(selectedTitle)
  })

})
