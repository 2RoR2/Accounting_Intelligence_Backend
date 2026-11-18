import { defineConfig } from '@playwright/test';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
export default defineConfig({
  testDir: './tests/browser',
  outputDir: join(tmpdir(), 'accounting-intelligence-playwright'),
  webServer: {
    command: 'npm run dev -- --port 3001',
    env: { NEXT_BUILD_DIR: '.next-review' },
    url: 'http://localhost:3001',
    reuseExistingServer: true,
    timeout: 120000,
  },
  timeout: 240000,
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  use: { baseURL: process.env.PLAYWRIGHT_BASE_URL ?? 'http://localhost:3001', channel: 'chrome', headless: true, actionTimeout: 20000, navigationTimeout: 60000, viewport: {width:1440,height:1000}, screenshot:'only-on-failure' },
});
