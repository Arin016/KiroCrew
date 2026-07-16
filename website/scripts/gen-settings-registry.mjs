#!/usr/bin/env node
/**
 * Generate settingsRegistry.gen.ts from settings panel source files.
 * Usage: node scripts/gen-settings-registry.mjs
 */
import { execSync } from 'child_process'
import * as fs from 'fs'
import * as path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')

const runnerScript = `
import { extractAll, generateRegistrySource } from './scripts/settingsExtract'
import * as path from 'path'
import * as fs from 'fs'

const settingsDir = path.resolve(${JSON.stringify(ROOT)}, 'src/pages/settings')
const { entries, skipped } = extractAll(settingsDir)
const outPath = path.resolve(${JSON.stringify(ROOT)}, 'src/components/commandPalette/settingsRegistry.gen.ts')
const source = generateRegistrySource(entries)
fs.writeFileSync(outPath, source)
console.log(\`Generated \${entries.length} entries (\${skipped} dynamic labels skipped) → settingsRegistry.gen.ts\`)
`

const tmpFile = path.join(ROOT, '.gen-settings-runner.ts')
fs.writeFileSync(tmpFile, runnerScript)

try {
  execSync(`npx vite-node "${tmpFile}"`, { cwd: ROOT, stdio: 'inherit' })
} finally {
  fs.unlinkSync(tmpFile)
}
