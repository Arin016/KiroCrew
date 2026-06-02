export async function knowledgeApi<T>(path: string, opts?: RequestInit): Promise<T> {
  const r = await fetch(`/api/knowledge${path}`, opts)
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
  return r.json()
}
