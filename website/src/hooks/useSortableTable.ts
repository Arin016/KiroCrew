import { useState, useMemo } from 'react'

export interface SortState { key: string | null; dir: 'asc' | 'desc' | null }

export function useSortableTable<T>(
  data: T[],
  tableId: string,
  comparators: Record<string, (a: T, b: T) => number>,
  defaultSort?: SortState,
) {
  const [sort, setSort] = useState<SortState>(() => {
    try {
      const raw = localStorage.getItem(`sort:${tableId}`)
      if (raw) return JSON.parse(raw)
    } catch { /* ignore */ }
    return defaultSort ?? { key: null, dir: null }
  })

  const toggle = (key: string) => {
    setSort(prev => {
      const next: SortState =
        prev.key !== key ? { key, dir: 'asc' }
        : prev.dir === 'asc' ? { key, dir: 'desc' }
        : prev.dir === 'desc' ? (defaultSort?.key === key && defaultSort?.dir === 'desc'
                                  ? { key, dir: 'asc' }
                                  : defaultSort ?? { key: null, dir: null })
        : { key, dir: 'asc' }
      try { localStorage.setItem(`sort:${tableId}`, JSON.stringify(next)) } catch { /* ignore */ }
      return next
    })
  }

  const sorted = useMemo(() => {
    const { key, dir } = sort
    if (!key || !dir || !comparators[key]) return data
    const cmp = comparators[key]
    return [...data].sort((a, b) => dir === 'asc' ? cmp(a, b) : cmp(b, a))
  }, [data, sort, comparators])

  return { sorted, sort, toggle }
}
