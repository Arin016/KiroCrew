import { test, expect } from '@playwright/test'

test.describe('Memory Graph Explorer E2E Tests', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/overview', { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: 'Memory' }).waitFor({ state: 'visible' })
  })

  test('Memory tab has Table and Graph view toggle', async ({ page }) => {
    await page.getByRole('button', { name: 'Memory' }).click()
    await expect(page.getByRole('button', { name: 'Table' })).toBeVisible({ timeout: 5000 })
    await expect(page.getByRole('button', { name: 'Graph' })).toBeVisible()
  })

  test('clicking Graph toggle renders the graph view', async ({ page }) => {
    await page.getByRole('button', { name: 'Memory' }).click()
    await page.getByRole('button', { name: 'Graph' }).click()
    await expect(page.getByText('Memory Graph')).toBeVisible({ timeout: 5000 })
  })

  test('Graph view shows filter buttons', async ({ page }) => {
    await page.getByRole('button', { name: 'Memory' }).click()
    await page.getByRole('button', { name: 'Graph' }).click()
    await expect(page.getByText(/All \(\d+\)/)).toBeVisible({ timeout: 5000 })
  })

  test('Graph view has a search input', async ({ page }) => {
    await page.getByRole('button', { name: 'Memory' }).click()
    await page.getByRole('button', { name: 'Graph' }).click()
    await expect(page.getByPlaceholder('Search nodes…')).toBeVisible({ timeout: 5000 })
  })

  test('can switch between Table and Graph views', async ({ page }) => {
    await page.getByRole('button', { name: 'Memory' }).click()

    // Default is Table view
    await expect(page.getByRole('heading', { name: /memory settings/i })).toBeVisible({ timeout: 5000 })

    // Switch to Graph
    await page.getByRole('button', { name: 'Graph' }).click()
    await expect(page.getByText('Memory Graph')).toBeVisible({ timeout: 5000 })

    // Switch back to Table
    await page.getByRole('button', { name: 'Table' }).click()
    await expect(page.getByRole('heading', { name: /memory settings/i })).toBeVisible({ timeout: 5000 })
  })
})
