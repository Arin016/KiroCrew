import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './playwright',
  fullyParallel: true, // Enable parallel execution
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined, // Use default workers (parallel) except in CI
  reporter: process.env.CI ? 'html' : 'list', // 'list' shows test names as they run
  timeout: 30000, // 30 second timeout per test
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:7777',
    trace: 'on-first-retry',
    video: process.env.PLAYWRIGHT_VIDEO === '1' ? 'on' : 'off',
    navigationTimeout: 10000, // 10 second navigation timeout
    actionTimeout: 10000, // 10 second action timeout
  },

  projects: [
    // Setup project: exchanges PLAYWRIGHT_TOKEN for a session cookie and
    // persists it to .auth/state.json. Keeps raw tokens out of test traces.
    { name: 'setup', testMatch: /auth\.setup\.ts/ },
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        storageState: process.env.PLAYWRIGHT_STORAGE_STATE || 'playwright/.auth/state.json',
      },
      dependencies: ['setup'],
    },
  ],

  // Note: Make sure kiroclaw gateway is running on port 7777 before running tests
  // Run: kiroclaw gateway
  webServer: undefined,
})
