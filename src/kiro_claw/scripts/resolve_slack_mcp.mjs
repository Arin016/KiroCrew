import { readFileSync, existsSync } from 'fs';
import { join } from 'path';
import { homedir } from 'os';

const CONFIG_PATHS = [
  join(homedir(), '.aim', 'config.json'),
  join(homedir(), '.config', 'smithy-mcp', 'config.json'),
];

export function resolveSlackMcpPath() {
  for (const cfgPath of CONFIG_PATHS) {
    try {
      const cfg = JSON.parse(readFileSync(cfgPath, 'utf8'));
      const p = cfg?.toolBundles?.['slack-mcp']?.brazilVSHosted?.resolvedArtifactPath;
      if (p && existsSync(p)) return p;
    } catch { /* file missing or unparseable — try next */ }
  }
  return undefined;
}
