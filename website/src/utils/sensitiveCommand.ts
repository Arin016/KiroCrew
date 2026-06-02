const CMD_PREFIX = '(command\\s+|builtin\\s+)?'
const FILE_READERS = `${CMD_PREFIX}(cat|less|head|tail|tac|more)`

const PER_LINE_PATTERNS: Array<{ pattern: RegExp; reason: string }> = [
  { pattern: />\s*\/dev\/tcp\//, reason: 'Opens raw TCP connection' },
  { pattern: /export\s+.*>(\/dev\/tcp|nc\s|netcat)/, reason: 'Sends data to network socket' },
  { pattern: new RegExp(`${FILE_READERS}\\s+.*\\.(aws|ssh|gnupg|config\\/gcloud)\\b`), reason: 'Reads credential files' },
  { pattern: new RegExp(`${FILE_READERS}\\s+.*\\/(credentials|id_rsa|id_ed25519|private\\.key)\\b`), reason: 'Reads credential files' },
  { pattern: new RegExp(`${FILE_READERS}\\s+(\\S*\\/)?\\.\\.?env\\b`), reason: 'Reads credential files' },
  { pattern: new RegExp(`${FILE_READERS}\\s+\\/etc\\/(shadow|passwd)`), reason: 'Reads system credentials' },
  { pattern: /env\s*\|\s*grep\s+.*(secret|key|token|pass|cred|aws)/i, reason: 'Dumps sensitive environment variables' },
  { pattern: /printenv\s+(AWS_SECRET|AWS_SESSION|GITHUB_TOKEN|NPM_TOKEN)/i, reason: 'Reads sensitive environment variable' },
  { pattern: /base64.*\.(aws|ssh|pem|key)\b/, reason: 'Encodes credential files' },
]

const FULL_BLOCK_PATTERNS: Array<{ pattern: RegExp; reason: string }> = [
  { pattern: /(curl|wget)\s[\s\S]*?\$\(/, reason: 'Sends command output to external URL' },
  { pattern: /(curl|wget)\s[\s\S]*?`[^`]*`/, reason: 'Sends command output to external URL' },
  { pattern: /(curl|wget)\s[\s\S]*?-d\s+@-/, reason: 'Pipes data to external URL' },
  { pattern: /(curl|wget)\s[\s\S]*?-d\s+@[^\s]/, reason: 'Uploads file to external URL' },
]

export interface SensitiveMatch {
  reason: string
}

export function checkSensitiveCommand(code: string): SensitiveMatch | null {
  for (const { pattern, reason } of PER_LINE_PATTERNS) {
    if (pattern.test(code)) return { reason }
  }
  for (const { pattern, reason } of FULL_BLOCK_PATTERNS) {
    if (pattern.test(code)) return { reason }
  }
  return null
}
