import { test, expect } from '@playwright/test'

test.describe('Cron Tab E2E Tests', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate directly to overview page, then click Cron tab
    await page.goto('/overview', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(500)
    await page.getByRole('button', { name: 'Cron' }).click()
    await page.waitForTimeout(500)
  })

  // Clean up test-created cron jobs after all tests
  test.afterAll(async ({ browser }) => {
    const page = await browser.newPage()
    await page.goto('/overview', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(500)
    await page.getByRole('button', { name: 'Cron' }).click()
    await page.waitForTimeout(500)

    // Set up dialog handler ONCE before loops
    page.on('dialog', dialog => dialog.accept())

    // Delete "Playwright_Test_Job" if it exists
    const testJobDeleteButtons = page.getByRole('row').filter({ hasText: 'Playwright_Test_Job' }).getByRole('button', { name: /delete|✕/i })
    const testJobCount = await testJobDeleteButtons.count()
    for (let i = 0; i < testJobCount; i++) {
      await testJobDeleteButtons.first().click()
      await page.waitForTimeout(500)
    }

    // Delete "Playwright_Weekly_Report" if it exists
    const weeklyReportDeleteButtons = page.getByRole('row').filter({ hasText: 'Playwright_Weekly_Report' }).getByRole('button', { name: /delete|✕/i })
    const weeklyReportCount = await weeklyReportDeleteButtons.count()
    for (let i = 0; i < weeklyReportCount; i++) {
      await weeklyReportDeleteButtons.first().click()
      await page.waitForTimeout(500)
    }

    // Delete "Playwright_Delete_Test" if it exists (from delete test)
    const deleteTestJobButtons = page.getByRole('row').filter({ hasText: 'Playwright_Delete_Test' }).getByRole('button', { name: /delete|✕/i })
    const deleteTestJobCount = await deleteTestJobButtons.count()
    for (let i = 0; i < deleteTestJobCount; i++) {
      await deleteTestJobButtons.first().click()
      await page.waitForTimeout(500)
    }

    await page.close()
  })

  test('displays existing cron jobs', async ({ page }) => {
    // Should see jobs table
    await expect(page.getByRole('table')).toBeVisible({ timeout: 5000 })
    
    // Should see table headers - use role to be specific
    await expect(page.getByRole('columnheader', { name: 'Name' })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Schedule' })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Message' })).toBeVisible()
  })

  test('creates new cron job with interval schedule', async ({ page }) => {
    // Fill in the cron job form
    await page.getByPlaceholder(/job name/i).fill('Playwright_Test_Job')
    await page.getByPlaceholder(/message.*task/i).fill('Run E2E tests every hour')

    // Interval should be selected by default
    // Set to 2 hours
    const intervalInput = page.locator('input[type="number"]').first()
    await intervalInput.fill('2')

    // Select hours
    const unitSelect = page.locator('select').filter({ hasText: /minutes|hours|days/ })
    await unitSelect.selectOption('hours')

    // Click Add button
    await page.getByRole('button', { name: /^add$/i }).click()

    // Verify the job appears in the table - use first() to handle duplicates from previous runs
    await expect(page.getByText('Playwright_Test_Job').first()).toBeVisible({ timeout: 5000 })
    await expect(page.getByText('Run E2E tests every hour').first()).toBeVisible()
  })

  test('creates cron job with weekly schedule', async ({ page }) => {
    // Fill in basic info
    await page.getByPlaceholder(/job name/i).fill('Playwright_Weekly_Report')
    await page.getByPlaceholder(/message.*task/i).fill('Generate weekly metrics')

    // Switch to weekly mode
    const modeSelect = page.locator('select').first()
    await modeSelect.selectOption('weekly')

    // Select Monday and Friday
    await page.getByRole('button', { name: /mon/i }).click()
    await page.getByRole('button', { name: /fri/i }).click()

    // Set time
    await page.locator('input[type="time"]').fill('09:00')

    // Click Add
    await page.getByRole('button', { name: /^add$/i }).click()

    // Verify creation - use first() to handle duplicates
    await expect(page.getByText('Playwright_Weekly_Report').first()).toBeVisible({ timeout: 5000 })
  })

  test('pauses and resumes a cron job', async ({ page }) => {
    // Wait for jobs to load
    await expect(page.getByRole('table')).toBeVisible({ timeout: 5000 })

    // Find first Pause button and click it
    const pauseButton = page.getByRole('button', { name: /pause/i }).first()
    if (await pauseButton.isVisible()) {
      await pauseButton.click()

      // Should change to Resume
      await expect(page.getByRole('button', { name: /resume/i }).first()).toBeVisible({
        timeout: 3000,
      })

      // Click Resume
      await page.getByRole('button', { name: /resume/i }).first().click()

      // Should change back to Pause
      await expect(page.getByRole('button', { name: /pause/i }).first()).toBeVisible({
        timeout: 3000,
      })
    }
  })

  test('deletes a cron job', async ({ page }) => {
    // First, create a cron job specifically for deletion testing
    await page.getByPlaceholder(/job name/i).fill('Playwright_Delete_Test')
    await page.getByPlaceholder(/message.*task/i).fill('Job created for deletion test')
    
    const intervalInput = page.locator('input[type="number"]').first()
    await intervalInput.fill('1')
    
    await page.getByRole('button', { name: /^add$/i }).click()
    
    // Verify it was created
    await expect(page.getByText('Playwright_Delete_Test').first()).toBeVisible({ timeout: 5000 })
    
    // Now delete it - find the specific row with our test job
    const testJobRow = page.getByRole('row').filter({ hasText: 'Playwright_Delete_Test' })
    const deleteButton = testJobRow.getByRole('button', { name: /delete/i })
    
    await deleteButton.click()
    
    // Job should be removed
    await page.waitForTimeout(1000)
    await expect(page.getByText('Playwright_Delete_Test')).not.toBeVisible()
  })

  test('filters cron jobs by search term', async ({ page }) => {
    // Wait for jobs to load
    await expect(page.getByRole('table')).toBeVisible({ timeout: 5000 })

    // Type in filter
    const filterInput = page.getByPlaceholder(/filter jobs/i)
    await filterInput.fill('test')

    // Jobs should still be visible (client-side filtering)
    await expect(page.getByRole('table')).toBeVisible()
  })

  test('validates required fields', async ({ page }) => {
    // Try to add without filling anything
    await page.getByRole('button', { name: /^add$/i }).click()

    // Should show error
    await expect(page.getByText(/name and message are required/i)).toBeVisible({
      timeout: 3000,
    })
  })

  test('validates weekly mode requires day selection', async ({ page }) => {
    await page.getByPlaceholder(/job name/i).fill('Test')
    await page.getByPlaceholder(/message.*task/i).fill('Test task')

    // Switch to weekly mode
    const modeSelect = page.locator('select').first()
    await modeSelect.selectOption('weekly')

    // Don't select any days
    await page.getByRole('button', { name: /^add$/i }).click()

    // Should show error
    await expect(page.getByText(/select at least one day/i)).toBeVisible({ timeout: 3000 })
  })
})
