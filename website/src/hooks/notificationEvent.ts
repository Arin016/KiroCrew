/** Shared contract between useSSE.ts (dispatcher) and useNotificationSound.ts (listener). */
export const MC_NOTIFICATION_EVENT = 'mc-notification' as const
export const MC_SOUND_SETTINGS_CHANGED_EVENT = 'mc-notification-sound-changed' as const

export interface McNotificationDetail {
  kind?: string
}
