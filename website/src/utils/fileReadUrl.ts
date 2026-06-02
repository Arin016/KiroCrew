/** Build the /api/file-read URL, appending resolve=1 for relative paths. */
export function fileReadUrl(filePath: string): string {
  const resolve = !filePath.startsWith('/') && !filePath.startsWith('~')
  return '/api/file-read?path=' + encodeURIComponent(filePath) + (resolve ? '&resolve=1' : '')
}
