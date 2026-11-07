import { defineConfig } from '@playwright/test'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
const dataDir = mkdtempSync(join(tmpdir(), 'music-discovery-browser-'))
export default defineConfig({
  testDir: './tests', timeout: 45000, workers: 1,
  use: { baseURL: 'http://127.0.0.1:8765', headless: true, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: { command: `../.venv/bin/python ../scripts/web_test_server.py --data-dir ${dataDir} --port 8765`, url: 'http://127.0.0.1:8765/', reuseExistingServer: false, timeout: 30000 },
})
